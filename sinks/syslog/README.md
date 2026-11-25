# syslog

metacomputer's code for syslog sinks: RFC 5424 messages over TLS with octet counting (RFC 5425), to
`syslog+tls://host:port`, up to 100 events a connection, for a SIEM or a syslog server. TLS only: there's no plain TCP or
UDP. Its page for people is [docs/events.md](../../docs/events.md#syslog-rfc-5424-over-tls) (the message, the structured
data, rsyslog's configuration); every kind is listed in [docs/sinks.md](../../docs/sinks.md), and
[sinks/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words and settings: 100 events a connection, no headers; `format`: the scheme, the default port, the structured data's SD-ID, the facility and the severity numbers, which the vault's audit sinks use too |
| `sink.py` | its URL's rules (`syslog+tls://HOST:PORT`, nothing more), the message (PRI, timestamp, HOSTNAME and MSGID as printable ASCII, the `[metacomputer@32473 ...]` element, the event as MSG) and its own transport: one TLS connection a batch, closed with close_notify (no acknowledgement: best effort) |
| `tests/vault_cases.json` | the cases its rules are checked against in both places, the stream's code and the vault's audit sinks (its URLs, PRI, escaping in the structured data, the MSGID field), with the differences the vault keeps on purpose and why |

Its tests: `tests/test_events.py` (its cases, a TLS receiver with its own CA: the message and its fields, the octet counts, a
receiver that resets the connection); `tests/test_vault_ops.py` runs its cases through the vault's own code.
