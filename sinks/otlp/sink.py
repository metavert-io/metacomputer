"""The OTLP sink: OpenTelemetry logs over OTLP/HTTP with the protobuf JSON mapping (POST URL/v1/logs), up to its
manifest's batch of events a request. People's docs: docs/events.md ("otlp"); this folder's README.md.

lib/events.py calls it, passing itself as E: check_url(E, u) for a sink's URL, request(E, sink, batch, extra) for what
one POST carries, accepted(E, answer, batch) for what a 2xx answer says (a collector may accept a request and refuse some
of its records: OTLP's partial success). Stdlib only."""
import json
import os
import time
from urllib.parse import urlsplit

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), encoding="utf-8") as _f:
    FORMAT = json.load(_f)["format"]           # what the vault's audit sinks send alike (lib/sinkkit/vault.py)
OTLP_SEV = {k: tuple(v) for k, v in FORMAT["severity"].items()}     # severity -> (severityNumber, severityText)
LOGS_PATH = FORMAT["path"]
CONTENT_TYPE = FORMAT["content_type"]


def check_url(E, u):
    E.require_https(u)


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}      # int64 as a string (protobuf JSON)
    return {"key": key, "value": {"stringValue": str(value)}}


def otlp_body(E, events):
    host = events[0]["host"] if events else E.host_name()
    observed = str(time.time_ns())
    recs = []
    for ev in events:
        num, text = OTLP_SEV[ev["severity"]]
        attrs = [_attr("metacomputer.family", ev["family"]), _attr("metacomputer.type", ev["type"]),
                 _attr("metacomputer.vm", ev["vm"]), _attr("metacomputer.seq", int(ev["seq"])),
                 _attr("metacomputer.origin", ev.get("origin") or "cli")]
        if ev.get("project"):
            attrs.append(_attr("metacomputer.project", ev["project"]))
        recs.append({"timeUnixNano": str(E.iso_to_ns(ev["time"])), "observedTimeUnixNano": observed,
                     "severityNumber": num, "severityText": text, "body": {"stringValue": E.dumps(ev)},
                     "attributes": attrs})
    return {"resourceLogs": [{
        "resource": {"attributes": [_attr("service.name", "metacomputer"), _attr("service.version", E.service_version()),
                                    _attr("host.name", host)]},
        "scopeLogs": [{"scope": {"name": "metacomputer.events", "version": "1"}, "logRecords": recs}]}]}


def otlp_rejected(E, body, sent):
    """(records the collector rejected, its message) from an OTLP/HTTP JSON answer: a 2xx with
    partialSuccess.rejectedLogRecords > 0 stored only part of the batch. OTLP says not to retry those."""
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return 0, ""
    ps = (d.get("partialSuccess") or d.get("partial_success")) if isinstance(d, dict) else None
    if not isinstance(ps, dict):
        return 0, ""
    raw = ps.get("rejectedLogRecords", ps.get("rejected_log_records", 0))   # an int64: a number or a string in JSON
    try:
        n = int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) else 0
    except ValueError:
        n = 0
    msg = ps.get("errorMessage") or ps.get("error_message") or ""
    return max(0, min(n, sent)), E.clean_str(msg, 160) if isinstance(msg, str) else ""


def otlp_url(url):
    """URL + /v1/logs, before any query string, unless it already ends with /v1/logs."""
    u = urlsplit(url)
    path = u.path.rstrip("/")
    path = path if path.endswith(LOGS_PATH) else path + LOGS_PATH
    return f"{u.scheme}://{u.netloc}{path}" + (f"?{u.query}" if u.query else "")


def request(E, sink, batch, extra):
    """(where to POST, the body, its headers) for a batch of events."""
    body = E.dumps(otlp_body(E, batch)).encode()
    headers = {**extra, "Content-Type": CONTENT_TYPE, "User-Agent": E.USER_AGENT}
    return otlp_url(sink["url"]), body, headers


def accepted(E, answer, batch):
    """None when the collector took every record; else the delivery's result: delivered, but with how many it refused
    (dropped, not retried, as OTLP says) and why."""
    n, msg = otlp_rejected(E, answer, len(batch))
    if n:
        return (True, f"the collector rejected {n} of {len(batch)} event(s) (OTLP partial success"
                + (f": {msg}" if msg else "") + "); not retried", False, n)
    return None
