"""metacomputer's sink framework: what shared code knows about the kinds of event sink, read from their folders
(sinks/<kind>/; sinks/README.md) so no shared program names one.

    registry.py      the kinds, an explicit list: each is a folder sinks/<kind>/ (its manifest, its code, its page, its
                     cases); loads a kind's code
    manifests.py     its manifest (sinks/<kind>/manifest.json): its words, its settings, what it carries, its docs, the
                     format it shares with the vault
    vault.py         what the vault's audit sinks share with the event stream, written as Swift for the vault's build

The rest of the framework is where it was, in lib/events.py: the events and their cleaning (the redaction core), the
limits, the spool, the backlogs and the delivery loop (deliver() calls a kind's sink.py), the watcher and the commands.

Two rules hold for every kind, and so live here: the header a signed request carries its signature in, and the names a
header may have. Works under any python3 (macOS's 3.9 included)."""

# A signed request's signature: "sha256=" and the hex HMAC-SHA256 of its body under the sink's key.
SIGNATURE_HEADER = "X-Metacomputer-Signature"
# The names --header takes (an HTTP token, at most 64 characters).
HEADER_NAME = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$"
