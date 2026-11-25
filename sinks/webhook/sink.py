"""The webhook sink: one CloudEvents 1.0 event per request (structured mode), signed: X-Metacomputer-Signature carries
sha256=HMAC-SHA256(body) under the sink's own key. People's docs: docs/events.md ("webhook"); this folder's README.md.

lib/events.py calls it, passing itself as E: check_url(E, u) for a sink's URL, request(E, sink, batch, extra) for what
one POST carries. Stdlib only."""
import base64
import hashlib
import hmac
import json
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), encoding="utf-8") as _f:
    FORMAT = json.load(_f)["format"]           # what the vault's audit sinks send alike (lib/sinkkit/vault.py)
CE_PREFIX = FORMAT["type_prefix"]
CONTENT_TYPE = FORMAT["content_type"]


def check_url(E, u):
    E.require_https(u)


def cloudevent(ev):
    return {"specversion": "1.0", "id": ev["id"], "source": f"metacomputer/{ev['host']}",
            "type": f"{CE_PREFIX}.{ev['family']}.{ev['type']}", "time": ev["time"], "subject": ev["vm"],
            "datacontenttype": "application/json", "data": ev}


def signature(key_b64, body):
    return "sha256=" + hmac.new(base64.b64decode(key_b64), body, hashlib.sha256).hexdigest()


def request(E, sink, batch, extra):
    """(where to POST, the body, its headers) for one event (the batch is one: its manifest's batch)."""
    ev = batch[0]
    body = E.dumps(cloudevent(ev)).encode()
    headers = {**extra, "Content-Type": CONTENT_TYPE, "User-Agent": E.USER_AGENT,
               E.SIGNATURE_HEADER: signature(sink["hmac_key"], body)}
    return sink["url"], body, headers
