#!/usr/bin/env python3
"""What the vault's audit sinks share with the event stream, from the sinks' manifests (sinks/<kind>/manifest.json), as
Swift: scripts/build-vault.sh compiles it with the vault's own files (`swift` below), so the vault keeps no second copy
of these. vault/Sinks.swift and vault/SinkCommands.swift read them; everything else of the vault's audit sinks is its
own (its socket, its records, what it signs, when it refuses) and stays in its Swift, checked against the same cases as
the stream's code (each kind's tests/vault_cases.json: tests/sink_cases.py).

    python3 lib/sinkkit/vault.py swift    the Swift (what build-vault.sh compiles in)
    python3 lib/sinkkit/vault.py check    what's wrong with the manifests for the vault, one line each (exit 1)

What's shared:
    sinkKinds                 the kinds the vault offers (`audit-sink add --kind`): those whose manifest has "vault", in
                              the registry's order
    sinkKindsWithoutAck       of those, the ones whose receivers don't acknowledge each record (never --required)
    sinkKindsWithoutHeaders   the ones that take no --header
    sinkSignatureHeader       the header a signed request carries its signature in
    sinkHeaderNamePattern     the names a header may have
    sinkContentTypes          each kind's Content-Type, for the kinds that POST
    sinkBatches               how many events one request (or connection) carries
    each kind's format        its manifest's "format", under the names below (FORMATS)
A manifest is a contributor's input: check() holds every value the Swift gets to a whitelist of its own (FORMATS' last
column: a path that can't change the URL's host, a port the vault's UInt16 holds, numbers in their ranges, words), and
swift() writes each as a Swift literal (lib/gensafe.py), so a value can't end its literal either. Works under any
python3 (macOS's 3.9 included)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gensafe  # noqa: E402
import sinkkit  # noqa: E402
from sinkkit import manifests, registry  # noqa: E402

# The kinds vault/Sinks.swift delivers (its deliverSink, cloudEventBody, otlpBody, syslogMessage, ntfyRequest): only
# these may say "vault" in their manifest. Another kind needs its delivery in the vault's Swift first, then a line here.
VAULT_DELIVERS = ("webhook", "otlp", "syslog", "ntfy")
SEVERITIES = ("info", "warn", "error")


def _each(test):
    """A check of a {severity: value} table: test on each value."""
    return lambda v: next((f"{{severity: {w}}}" for w in (test(x) for x in v.values()) if w), None)


def _word(pattern, what, limit=64):
    return lambda v: gensafe.matches(v, pattern, what, limit)


_TAG = re.compile(r"[a-z0-9_+-]{1,64}")
# Each kind's format, as the Swift names it: (kind, key in its manifest's format, Swift name, Swift type, its whitelist:
# None when the value is one, else what it should be).
FORMATS = (
    ("webhook", "type_prefix", "cloudEventsTypePrefix", "String",
     _word(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", "a dotted name (lowercase letters, digits, . and -)", 128)),
    # appended to a sink's URL: it mustn't be able to add a user, a host or a port to it
    ("otlp", "path", "otlpLogsPath", "String", gensafe.url_path),
    ("otlp", "severity", "otlpSeverity", "[String: (number: Int, text: String)]",
     _each(lambda x: gensafe.number(x[0], 1, 24) and "[a number from 1 to 24, TEXT]"
           or gensafe.matches(x[1], r"[A-Za-z][A-Za-z0-9_]*", "[NUMBER, a word of letters, digits and _]", 32))),
    ("syslog", "scheme", "syslogScheme", "String", _word(r"[a-z][a-z0-9+.-]*", "a URL scheme", 32)),
    # the vault converts it to a UInt16
    ("syslog", "port", "syslogDefaultPort", "Int", gensafe.port),
    ("syslog", "sd_id", "syslogSDID", "String", gensafe.sd_id),
    ("syslog", "facility", "syslogFacility", "Int", lambda v: gensafe.number(v, 0, 23)),
    ("syslog", "severity", "syslogSeverity", "[String: Int]", _each(lambda x: gensafe.number(x, 0, 7))),
    ("ntfy", "priority", "ntfyPriority", "[String: Int]", _each(lambda x: gensafe.number(x, 1, 5))),
    ("ntfy", "severity_tags", "ntfySeverityTags", "[String: [String]]",
     _each(lambda x: None if len(x) <= 8 and all(_TAG.fullmatch(t) for t in x)
           else "at most 8 tags, each of lowercase letters, digits and _ + -")),
)


def vault_kinds():
    return [k for k in registry.SINKS if manifests.manifest(k).get("vault")]


def check():
    """What's wrong with the manifests for the vault: [str] (empty: nothing)."""
    bad = []
    for k in registry.SINKS:
        bad += [f"sinks/{k}/manifest.json: {b}" for b in manifests.check(k)]
    if bad:
        return bad
    kinds = vault_kinds()
    if tuple(kinds) != VAULT_DELIVERS:
        bad.append(f"the kinds with \"vault\" in their manifest ({', '.join(kinds) or 'none'}) aren't the ones "
                   f"vault/Sinks.swift delivers ({', '.join(VAULT_DELIVERS)})")
    for kind, key, name, typ, whitelist in FORMATS:
        fmt = manifests.manifest(kind)["format"] if kind in registry.SINKS else {}
        v = fmt.get(key)
        ok = {"String": isinstance(v, str) and v != "",
              "Int": isinstance(v, int) and not isinstance(v, bool) and v > 0,
              "[String: Int]": isinstance(v, dict) and set(v) == set(SEVERITIES)
              and all(isinstance(x, int) and not isinstance(x, bool) for x in v.values()),
              "[String: [String]]": isinstance(v, dict) and set(v) <= set(SEVERITIES)
              and all(isinstance(x, list) and all(isinstance(t, str) for t in x) for x in v.values()),
              "[String: (number: Int, text: String)]": isinstance(v, dict) and set(v) == set(SEVERITIES)
              and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], int) and isinstance(x[1], str)
                      for x in v.values())}[typ]
        if not ok:
            bad.append(f"sinks/{kind}/manifest.json: format.{key} isn't what the vault reads as {name} ({typ})")
        elif whitelist(v):
            bad.append(f"sinks/{kind}/manifest.json: format.{key} isn't {whitelist(v)}")
    for k in kinds:
        if k != "syslog" and not manifests.manifest(k)["format"].get("content_type"):
            bad.append(f"sinks/{k}/manifest.json: format.content_type is missing (the vault posts it)")
    if not re.fullmatch(sinkkit.HEADER_NAME, sinkkit.SIGNATURE_HEADER):
        bad.append("lib/sinkkit/__init__.py: SIGNATURE_HEADER isn't a header's name (HEADER_NAME)")
    return bad


_s = gensafe.swift_string        # a Swift string literal: \ and " escaped, anything outside printable ASCII as \u{...}
_i = gensafe.swift_int           # a Swift Int literal (ValueError for anything but a whole number)


def _value(v, typ):
    if typ == "String":
        return _s(v)
    if typ == "Int":
        return _i(v)
    if typ == "[String: Int]":
        return "[" + ", ".join(f"{_s(k)}: {_i(v[k])}" for k in SEVERITIES if k in v) + "]"
    if typ == "[String: [String]]":
        return "[" + ", ".join(f"{_s(k)}: [" + ", ".join(_s(t) for t in v[k]) + "]" for k in SEVERITIES if k in v) + "]"
    return "[" + ", ".join(f"{_s(k)}: ({_i(v[k][0])}, {_s(v[k][1])})" for k in SEVERITIES if k in v) + "]"


def swift():
    """The shared definitions as Swift source (scripts/build-vault.sh compiles it with the vault's own files)."""
    kinds = vault_kinds()

    def lst(xs):
        return "[" + ", ".join(_s(x) for x in xs) + "]"
    posts = [k for k in kinds if manifests.manifest(k)["format"].get("content_type")]
    lines = [
        "// Generated by scripts/build-vault.sh from the sinks' manifests (lib/sinkkit/vault.py swift): what the vault's",
        "// audit sinks send alike with the event stream. Not kept in the repository.",
        f"let sinkKinds = {lst(kinds)}",
        f"let sinkKindsWithoutAck = {lst(k for k in kinds if not manifests.manifest(k)['vault']['acknowledges'])}",
        f"let sinkKindsWithoutHeaders = {lst(k for k in kinds if not manifests.manifest(k)['vault']['headers'])}",
        f"let sinkSignatureHeader = {_s(sinkkit.SIGNATURE_HEADER)}",
        f"let sinkHeaderNamePattern = {_s(sinkkit.HEADER_NAME)}",
        "let sinkContentTypes: [String: String] = ["
        + ", ".join(f"{_s(k)}: {_s(manifests.manifest(k)['format']['content_type'])}" for k in posts) + "]",
        "let sinkBatches: [String: Int] = ["
        + ", ".join(f"{_s(k)}: {_i(manifests.manifest(k)['batch'])}" for k in kinds) + "]",
    ]
    for kind, key, name, typ, _whitelist in FORMATS:
        lines.append(f"let {name}: {typ} = {_value(manifests.manifest(kind)['format'][key], typ)}")
    return "\n".join(lines) + "\n"


def main(argv):
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    what = strictargs.expect(argv, ["swift|check"], prog="vault.py", usage=__doc__).words[0]
    bad = check()
    if what == "check":
        for b in bad:
            print(b)
        return 1 if bad else 0
    if bad:
        print("vault.py: " + "; ".join(bad), file=sys.stderr)
        return 1
    sys.stdout.write(swift())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
