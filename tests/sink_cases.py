"""The cases the event stream's sink code (sinks/<kind>/sink.py, lib/events.py) and the vault's own copy of the same
rules (vault/Sinks.swift, through a TEST build's `test-sink-cases`) are both checked against, ready to use: the shared
ones (tests/fixtures/sink_cases.json: the header names every kind takes) and each kind's own
(sinks/<kind>/tests/vault_cases.json).

A case is {"fn": the rule, "in": {"s": its text, and what else the rule takes}, "out": what both answer} or, where the
vault answers otherwise on purpose, {"out": the stream's answer, "vault": the vault's, "why": the reason}: a declared
difference (each "why" says which side keeps its own rule, and why it isn't shared). A signing key is never written in a
case, only described ("key_bytes": "A..B", the bytes A to B): key() makes it, for the stream and for the vault alike.

    cases()                         every case, with "kind" (its folder, or "" for the shared ones) and "where"
    stream(case)                    the stream's answer
    vault_answers(binary, cases)    the vault's answers (its test-sink-cases), in order
tests/test_events.py checks the stream against every case's "out"; tests/test_vault_ops.py the vault against "vault"
or "out"."""
import base64
import json
import os
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(LAB, "lib"))
from sinkkit import registry  # noqa: E402

SHARED = os.path.join(TESTS, "fixtures", "sink_cases.json")
FNS = {"": ("header_name",),
       "webhook": ("url_ok", "signature", "cloudevent_type"),
       "otlp": ("url_ok", "otlp_url", "otlp_severity", "time_ns", "rejected"),
       "syslog": ("url_ok", "syslog_pri", "sd_escape", "syslog_field"),
       "ntfy": ("url_ok", "ntfy_severity", "ntfy_endpoint")}


def files():
    """[(kind, path)]: the shared cases, then each registered kind's (its folder's tests/vault_cases.json, if any)."""
    out = [("", SHARED)]
    for k in registry.SINKS:
        p = os.path.join(registry.folder(k), "tests", "vault_cases.json")
        if os.path.isfile(p):
            out.append((k, p))
    return out


def key(i):
    """The base64 signing key a case's "key_bytes" ("A..B") describes: the bytes A to B."""
    a, _, b = str(i["key_bytes"]).partition("..")
    return base64.b64encode(bytes(range(int(a), int(b) + 1))).decode()


def vault_input(c):
    """A case's "in" as the vault's test-sink-cases reads it: a described key made (it reads "key")."""
    i = dict(c.get("in") or {})
    if "key_bytes" in i:
        i["key"] = key(i)
        del i["key_bytes"]
    return i


def cases():
    out = []
    for kind, path in files():
        with open(path, encoding="utf-8") as f:
            for i, c in enumerate(json.load(f)):
                out.append(dict(c, kind=kind, where=f"{os.path.relpath(path, LAB)}[{i}]"))
    return out


def stream(c):
    """The stream's answer to case c."""
    import events                   # here: only the stream's checks need it (they run with test_events' folders)
    fn, i = c["fn"], c.get("in") or {}
    s = i.get("s", "")
    mod = registry.module(c["kind"]) if c["kind"] else None
    if fn == "header_name":
        return bool(events.HEADER_NAME_RE.fullmatch(s))
    if fn == "url_ok":
        try:
            events.check_url(c["kind"], s)
            return True
        except events.EventsError:
            return False
    if fn == "signature":
        return mod.signature(key(i), s.encode())
    if fn == "cloudevent_type":
        return mod.cloudevent({"id": "x", "host": "h", "family": "vault", "type": s, "time": "t", "vm": "v"})["type"]
    if fn == "otlp_url":
        return mod.otlp_url(s)
    if fn == "otlp_severity":
        return list(mod.OTLP_SEV[s])
    if fn == "time_ns":
        return str(events.iso_to_ns(s))
    if fn == "rejected":
        return mod.otlp_rejected(events, s.encode(), 10**9)[0]
    if fn == "syslog_pri":
        return mod.SYSLOG_FACILITY * 8 + mod.SYSLOG_SEV[s]
    if fn == "sd_escape":
        return mod._sd_value(s)
    if fn == "syslog_field":
        return mod._ascii(s, i["max"])
    if fn == "ntfy_severity":
        return [mod.PRIORITY[s], list(mod.SEVERITY_TAGS.get(s, []))]
    if fn == "ntfy_endpoint":
        ev = {"vm": "v", "family": "lifecycle", "type": "x", "severity": "info", "message": "m", "origin": "cli"}
        endpoint, payload = mod.ntfy_payload(events, s, ev)
        return [endpoint, payload["topic"]]
    raise ValueError(f"{c['where']}: unknown fn {fn!r}")


def vault_answers(binary, cs, env):
    """The vault's answers to cases cs (a TEST build's test-sink-cases), in order; None for each when it can't run."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([{"fn": c["fn"], "kind": c["kind"], "in": vault_input(c)} for c in cs], f)
    try:
        r = subprocess.run([binary, "test-sink-cases", f.name], env=env, capture_output=True, text=True, timeout=60)
    finally:
        os.remove(f.name)
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) != len(cs):
        return [None] * len(cs), r
    return [json.loads(x)["out"] for x in lines], r


def check_shape(c):
    """What's wrong with a case's form: [str]."""
    bad = []
    if set(c) - {"fn", "in", "out", "vault", "why", "kind", "where"}:
        bad.append(f"{c['where']}: keys {sorted(set(c) - {'fn', 'in', 'out', 'vault', 'why', 'kind', 'where'})}")
    if c.get("fn") not in FNS.get(c["kind"], ()):
        bad.append(f"{c['where']}: fn {c.get('fn')!r} isn't one of this folder's ({', '.join(FNS.get(c['kind'], ()))})")
    if "out" not in c:
        bad.append(f"{c['where']}: no \"out\"")
    if ("vault" in c) != bool(c.get("why")):
        bad.append(f"{c['where']}: a declared difference has both \"vault\" and its \"why\"")
    return bad
