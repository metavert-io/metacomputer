"""The one HTTPS call a provider's REST API needs (the providers on lib/cloud/vps.py), kept out of lib/cloud and the
providers' adapters, which import nothing that reaches the network: lib/remote.py hands send() to the adapter it
makes, and tests hand in a stand-in.

    send(method, url, headers, body, timeout) -> (status, headers, body bytes)

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
