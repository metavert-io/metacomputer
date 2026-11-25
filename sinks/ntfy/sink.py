"""The ntfy sink: one short message per event for a person's phone, through ntfy's JSON publishing (POST to the server's
root, the topic in the body). People's docs: docs/events.md ("ntfy"); this folder's README.md.

lib/events.py calls it, passing itself as E: check_url(E, u) for a sink's URL, request(E, sink, batch, extra) for what
one POST carries. Its manifest says it's a phone: each ntfy sink has its own cap (lib/events.py PHONE_LIMITS). Stdlib
only."""
import json
import os
from urllib.parse import urlsplit

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), encoding="utf-8") as _f:
    FORMAT = json.load(_f)["format"]           # what the vault's audit sinks send alike (lib/sinkkit/vault.py)
CONTENT_TYPE = FORMAT["content_type"]
PRIORITY = FORMAT["priority"]                  # by severity; a needs_you event is always 5
SEVERITY_TAGS = FORMAT["severity_tags"]        # tags a severity adds after the family's
NTFY_TAGS = {"needs_you": "raising_hand", "task": "white_check_mark", "security": "shield", "lifecycle": "computer",
             "actions": "memo"}


def check_url(E, u):
    E.require_https(u)
    if not u.path.strip("/"):
        raise E.EventsError("an ntfy URL names the topic: https://ntfy.sh/YOUR-TOPIC")


def ntfy_title(E, ev):
    return f"{ev['vm']} - {E.FAMILY_LABEL[ev['family']]}: {ev['type'].replace('_', ' ')}"


def ntfy_payload(E, url, ev):
    u = urlsplit(url)
    base, _, topic = u.path.rstrip("/").rpartition("/")
    prio = 5 if ev["family"] == "needs_you" else PRIORITY[ev["severity"]]
    tags = [NTFY_TAGS[ev["family"]]] + next((list(t) for sev, t in SEVERITY_TAGS.items() if ev["severity"] == sev), [])
    msg = ev["message"] + (f" -> {ev['action']}" if ev.get("action") else "")
    msg = E.PROVENANCE.get(ev.get("origin"), "") + msg     # say where it came from when anything could have sent it
    endpoint = f"{u.scheme}://{u.netloc}{base}/" + (f"?{u.query}" if u.query else "")   # keeps ?auth=
    return endpoint, {"topic": topic, "title": ntfy_title(E, ev), "message": msg, "priority": prio, "tags": tags}


def request(E, sink, batch, extra):
    """(where to POST, the body, its headers) for one event (the batch is one: its manifest's batch)."""
    target, payload = ntfy_payload(E, sink["url"], batch[0])
    body = E.dumps(payload).encode()
    headers = {**extra, "Content-Type": CONTENT_TYPE, "User-Agent": E.USER_AGENT}
    return target, body, headers
