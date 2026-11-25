"""The syslog sink: RFC 5424 messages over TLS with octet counting (RFC 5425), to syslog+tls://host:port, up to its
manifest's batch of events a connection. People's docs: docs/events.md ("syslog"); this folder's README.md.

lib/events.py calls it, passing itself as E: check_url(E, u) for a sink's URL, deliver(E, sink, batch, timeout, track)
to send a batch (a transport of its own, not an HTTP POST). Stdlib only."""
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), encoding="utf-8") as _f:
    FORMAT = json.load(_f)["format"]           # what the vault's audit sinks send alike (lib/sinkkit/vault.py)
SCHEME = FORMAT["scheme"]
PORT = FORMAT["port"]
SD_ID = FORMAT["sd_id"]                        # metacomputer@32473: the documentation enterprise number (RFC 5612)
SYSLOG_FACILITY = FORMAT["facility"]           # user-level
SYSLOG_SEV = FORMAT["severity"]


def check_url(E, u):
    if u.scheme != SCHEME or not u.hostname:
        raise E.EventsError("syslog sinks are TLS only: syslog+tls://HOST:PORT (RFC 5425; port 6514 by default)")
    if u.path not in ("", "/") or u.query:
        raise E.EventsError("a syslog URL is just syslog+tls://HOST:PORT")


def _sd_value(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _ascii(s, limit):
    s = "".join(c for c in str(s) if 33 <= ord(c) <= 126)
    return s[:limit] or "-"


def syslog_frame(E, ev):
    pri = SYSLOG_FACILITY * 8 + SYSLOG_SEV[ev["severity"]]
    sd = (f'[{SD_ID} seq="{ev["seq"]}" family="{ev["family"]}" origin="{_sd_value(ev.get("origin") or "cli")}" '
          f'vm="{_sd_value(ev["vm"])}" id="{ev["id"]}"]')
    msg = (f"<{pri}>1 {ev['time']} {_ascii(ev['host'], 255)} metacomputer - {_ascii(ev['type'], 32)} {sd} "
           + E.dumps(ev)).encode("utf-8")
    return str(len(msg)).encode() + b" " + msg


def deliver(E, sink, batch, timeout, track):
    """Send the batch over one TLS connection: (ok, error, permanent, rejected), as lib/events.py's deliver().
    Syslog over TCP has no acknowledgement: success means TLS took every byte and the connection closed without a
    transport or TLS error (no close_notify back, or the server just closing, is fine). A server that drops messages
    after that isn't noticed (best effort). track (a list) gets a second descriptor of the socket: shutting it down cuts
    the connection at the caller's deadline, TLS or not."""
    u = urlsplit(sink["url"])
    try:
        raw = socket.create_connection((u.hostname, u.port or PORT), timeout=timeout)
        track.append(raw.dup())
        try:
            tls = E.tls_context(sink.get("ca_pem")).wrap_socket(raw, server_hostname=u.hostname)
            tls.sendall(b"".join(syslog_frame(E, ev) for ev in batch))
            try:
                tls.settimeout(0.5)
                tls.unwrap()                 # close_notify, so the server sees a clean end
            except (socket.timeout, ssl.SSLZeroReturnError, ssl.SSLEOFError):
                pass
            except (OSError, ssl.SSLError) as e:    # a reset or a TLS alert: it wasn't taken cleanly
                return False, E.short_error(e), False, 0
            tls.close()
        finally:
            raw.close()
        return True, None, False, 0
    except (OSError, ssl.SSLError, ValueError) as e:
        return False, E.short_error(e), False, 0
