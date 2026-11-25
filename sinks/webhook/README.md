# webhook

metacomputer's code for webhook sinks: one HTTPS `POST` per event, a CloudEvents 1.0 event in structured mode, signed with
the sink's own key. Its page for people is [docs/events.md](../../docs/events.md#webhook-cloudevents-10-signed) (the format,
verifying the signature, replays, relaying to a chat tool); every kind is listed in [docs/sinks.md](../../docs/sinks.md),
and [sinks/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words and settings: one event a request, headers taken, `signed` (each sink gets a 32-byte key, printed once by `sink add` and again by `sink show NAME --key`), the path words of its URLs that are never secret; `format`: the content type and the CloudEvents type prefix, which the vault's audit sinks use too |
| `sink.py` | its URL's rules (https, or http only to this Mac's loopback), the CloudEvent, and the request: `X-Metacomputer-Signature: sha256=HMAC-SHA256(body)` |
| `tests/vault_cases.json` | the cases its rules are checked against in both places, the stream's code and the vault's audit sinks (its URLs, the signature, the CloudEvents type), with the differences the vault keeps on purpose and why |

Its tests: `tests/test_events.py` (its cases, the CloudEvent, its signature over the exact body, the headers, a receiver that's down
or refuses, sink changes announced to it); `tests/test_vault_ops.py` runs its cases through the vault's own code.
