# otlp

metacomputer's code for OTLP sinks: OpenTelemetry logs over OTLP/HTTP, in the protobuf JSON mapping (`POST <url>/v1/logs`),
up to 50 events a request, for an OpenTelemetry Collector or any backend that takes OTLP. Its page for people is
[docs/events.md](../../docs/events.md#otlp-opentelemetry-logs-over-otlphttp-json) (the request, the attributes, a
Collector's configuration); every kind is listed in [docs/sinks.md](../../docs/sinks.md), and
[sinks/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words and settings: 50 events a request, headers taken (a collector's token), the path words of its URLs that are never secret, its words for a partial success; `format`: the content type, the `/v1/logs` path and the severity numbers and texts, which the vault's audit sinks use too |
| `sink.py` | its URL's rules (https, or http only to this Mac's loopback), `/v1/logs` added before any query, the logs request (resource, scope, one log record per event, its attributes), and what a 2xx answer says: a `partialSuccess` with rejected records is delivered, those records dropped, not retried |
| `tests/vault_cases.json` | the cases its rules are checked against in both places, the stream's code and the vault's audit sinks (its URLs, `/v1/logs`, the severities, the time, a partial success's count), with the differences the vault keeps on purpose and why |

Its tests: `tests/test_events.py` (its cases, the request's shape, batches, a record the collector refuses inside a batch, partial
success, a receiver that's down); `tests/test_vault_ops.py` runs its cases through the vault's own code.
