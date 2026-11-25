# ntfy

metacomputer's code for ntfy sinks: one short message per event for a person's phone, through [ntfy](https://ntfy.sh)'s
JSON publishing (`POST` to the server's root, the topic in the body). Its page for people is
[docs/events.md](../../docs/events.md#ntfy-a-message-for-a-person) (the message, priorities and tags, a topic of your own,
`?auth=`); every kind is listed in [docs/sinks.md](../../docs/sinks.md), and [sinks/README.md](../README.md) says what a
folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words and settings: one event a request, headers taken (an access token), `phone` (each ntfy sink has its own cap, and sink-change announcements waiting for it go as one message), the path words of its URLs that are never secret; `format`: the content type, the priority by severity and the tags a severity adds, which the vault's audit sinks use too |
| `sink.py` | its URL's rules (https, or http only to this Mac's loopback, and a topic), the message (title, text with the action and where it came from, priority 5 for needs_you, tags) and the request, which keeps an `?auth=` query |

Its tests: `tests/test_events.py` (the message, its priority and tags, `?auth=`, the phone's cap, several announcements
in one message).
