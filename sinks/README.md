# Event sinks

Each kind of place the event stream can send its events to (`metacomputer events sink add NAME --kind KIND`) is a folder
here. A pull request for one kind should change that folder and little else: shared code never names a kind (a test
fails when it does: tests/test_events.py), and each folder has its own reviewers (`.github/CODEOWNERS`).

The list of kinds, what each sends and where its docs are: [docs/sinks.md](../docs/sinks.md) (made from the manifests).
How the event stream works, and each kind's format for the people who receive it: [docs/events.md](../docs/events.md).

## What a kind's folder holds

```
sinks/<kind>/
  manifest.json        its words and settings (lib/sinkkit/manifests.py): label, help (how `metacomputer help` lists it),
                       about (what it sends, in a line), docs (its section for people), owners (CODEOWNERS); batch (events
                       a request), headers (takes --header), signed (each sink gets a signing key), phone (a phone's cap
                       applies), url_words (path segments of its URLs that are never secret), partial (its words for a
                       request accepted with some events refused); format (what the vault's audit sinks send alike: the
                       vault's build compiles it in); vault (present when the vault's audit sinks have the kind too)
  sink.py              its code, which lib/events.py calls, passing itself as E (see The interface)
  README.md            its page for contributors: what's where, its tests (people's docs stay in docs/events.md)
  tests/vault_cases.json   optional: cases for the rules it shares with the vault's audit sinks, which both are checked
                       against (tests/sink_cases.py), with any difference the vault keeps and why
```

The framework is `lib/sinkkit/` (the registry, the manifests, the rules every kind follows, and `vault.py`, what the
vault's build compiles in) and `lib/events.py`, which calls into the folders: the events and their cleaning, the limits,
the spool, each sink's backlog, retries and backoff, the delivery loop and its deadline, the announcements of sink
changes, the watcher and the commands.

## Adding a kind

1. Its folder, as above. Start from the closest one: a kind that POSTs one event a request is a `request()` (`webhook`,
   `ntfy`); one that batches adds `batch` (`otlp`); one with a transport of its own brings `deliver()` (`syslog`).
2. One line in `lib/sinkkit/registry.py` (`SINKS`: its order is the order they're offered in, nothing else).
3. `python3 scripts/gen-sinks.py`: docs/sinks.md, `metacomputer help`'s line for the event stream and CODEOWNERS follow
   from the manifests.
4. A line in the CHANGELOG.

A new kind is the event stream's only, unless its manifest says `vault`: the vault's audit sinks deliver in their own
Swift (vault/Sinks.swift), so a kind for them needs that code first, and a line in `lib/sinkkit/vault.py`.

The checks: `python3 tests/test_events.py` (every folder's shape, the manifests, the generated files being current, no
kind named in shared code, every case in the folders, and the stream's behaviour end to end against stand-in receivers),
and `python3 tests/test_vault_ops.py` for the vault's side of the cases.

## The interface

`lib/events.py` finds a kind's behaviour by what its `sink.py` brings, never by its name, and passes itself as `E`:

- `check_url(E, u)`: the kind's rules for its URL (`u` is `urllib.parse.urlsplit`'s), after the ones every kind has
  (no credentials in it, no fragment, a port); raise `E.EventsError` with what's wrong. A kind that posts over HTTP calls
  `E.require_https(u)` (https, or http only to this Mac's loopback).
- `request(E, sink, batch, extra)`: for a kind that POSTs, (the URL, the body, the headers) for one request; `extra` is
  the sink's own headers. lib/events.py sends it (no redirects, no proxies, within the deadline): a 2xx is delivered;
  400, 413, 415 and 422 refuse the batch for good (it's sent again one event at a time, and an event refused three
  times is dropped); anything else is retried with backoff.
- `accepted(E, answer, batch)`: optional, for a kind whose 2xx answers say more: None, or what the delivery was
  (`(True, why, False, refused)`: delivered, with `refused` of its events dropped, not retried).
- `deliver(E, sink, batch, timeout, track)`: for a kind with a transport of its own instead of `request`: send the batch
  within `timeout`, putting a second descriptor of each socket in `track` (so the deadline can cut it), and return
  `(ok, error, permanent, refused)`.

What a kind may use from `E`: `dumps` (the queue's JSON), `clean_str` (the cleaning every event got), `host_name`,
`iso_to_ns`, `service_version`, `FAMILY_LABEL`, `PROVENANCE` (how a message for a person says where an event came
from), `USER_AGENT`, `SIGNATURE_HEADER`, `tls_context`, `short_error`, `require_https`, `EventsError`. A kind's
`sink.py` reads its own manifest's `format`.

Everything the guards decide stays shared and unchanged: what an event may hold and how it's cleaned (before any kind
sees it), the limits, which sinks get which events, the announcements of sink changes, who may change the sinks (only
from a terminal), and the vault's audit sinks' rules.
