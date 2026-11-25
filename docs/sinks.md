# Event sinks

<!-- Made from sinks/<kind>/manifest.json by scripts/gen-sinks.py: don't edit by hand (its --check, run by tests/test_events.py, fails a copy that isn't current). -->

The kinds of sink `metacomputer events sink add NAME --kind KIND --url URL` takes ([docs/events.md](events.md): what the event stream sends, when and how, and each kind's format). Each kind is a folder of its own, `sinks/<kind>/` ([sinks/README.md](../sinks/README.md)), listed in `lib/sinkkit/registry.py`. The secrets vault's audit sinks (`metacomputer secrets audit-sink add`) have the kinds marked so in their column, in the same wrappings around the vault's own records, which they sign ([SECURITY.md](../SECURITY.md)); one that must acknowledge every record (`--required`) can't be of a kind whose receivers don't.

- **Events a request**: at most this many in one request (or one connection).
- **Signed**: the event stream gives each sink of the kind a key of its own, and every request carries `sha256=HMAC(body)` under it.
- **A phone**: a sink for a person's phone has its own cap (10 in any 10 minutes, 30 in any hour), and announcements of sink changes waiting for it go as one message.

| Kind | What it sends | Events a request | Headers | Signed | A phone | The vault's audit sinks | Docs | Code |
|---|---|---|---|---|---|---|---|---|
| `webhook` | CloudEvents 1.0 structured JSON, signed: X-Metacomputer-Signature: sha256=HMAC(body) | 1 | yes | yes | no | yes | [docs/events.md](events.md#webhook-cloudevents-10-signed) | [sinks/webhook/](../sinks/webhook/) |
| `otlp` | OpenTelemetry logs, OTLP/HTTP JSON (POST URL/v1/logs) | 50 | yes | no | no | yes | [docs/events.md](events.md#otlp-opentelemetry-logs-over-otlphttp-json) | [sinks/otlp/](../sinks/otlp/) |
| `syslog` | RFC 5424 over TLS with octet counting (RFC 5425), syslog+tls://host:port | 100 | no | no | no | yes (never --required) | [docs/events.md](events.md#syslog-rfc-5424-over-tls) | [sinks/syslog/](../sinks/syslog/) |
| `ntfy` | one short human message per event (https; http only to this Mac's loopback) | 1 | yes | no | yes | yes (never --required) | [docs/events.md](events.md#ntfy-a-message-for-a-person) | [sinks/ntfy/](../sinks/ntfy/) |
