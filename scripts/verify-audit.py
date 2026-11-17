#!/usr/bin/env python3
"""verify-audit.py: check metacomputer secrets' audit events wherever they ended up.

    verify-audit.py [--key KEY | --key-file FILE] FILE...

Each FILE may be (detected line by line, mixed files are fine):
  - the vault's own log, audit.jsonl (one event per line);
  - what a webhook sink received: JSON lines {"signature": "sha256=...", "body": "<the request body>"},
    each body a CloudEvent whose `data` is the event;
  - what an OTLP sink received: the same {"signature", "body"} lines, each body an OTLP/HTTP JSON
    logs request; every log record carries its own metacomputer.signature attribute;
  - what a syslog sink received: one RFC 5424 message per line (octet count removed), the event as
    MSG and its signature in the [metacomputer@32473 ... sig="sha256=..."] element.
The ntfy sink's one-line summaries carry no signature and aren't accepted.

Checks: every event's hash (SHA-256 of the event's canonical JSON without "hash"), the chain (each
event's "prev" is the previous event's hash), gaps and conflicting duplicates in "seq", and, with the
sink's key, every HMAC-SHA256 signature. The key is what `metacomputer secrets audit-sink add` printed
once: 32 bytes in base64 (hex is accepted too); the HMAC uses the decoded bytes. A signed capture
needs the key. A repeated delivery of the same event (same seq, same hash) is fine.
Exit 0 when everything checks out, 1 when something doesn't, 2 for usage errors. Standard library only.
"""
import argparse, base64, binascii, hashlib, hmac, json, re, sys

SD_ID = "metacomputer@32473"


def parse_key(text):
    """A sink key: base64 (as printed) or 64 hex digits; the 32 bytes either way."""
    t = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", t):
        return bytes.fromhex(t)
    try:
        k = base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not base64 or hex")
    if len(k) != 32:
        raise ValueError(f"{len(k)} bytes, not 32")
    return k


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def event_hash(ev):
    return hashlib.sha256(canonical({k: v for k, v in ev.items() if k != "hash"}).encode()).hexdigest()


def sig_ok(key, data, sig):
    want = "sha256=" + hmac.new(key, data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig or "")


class Checker:
    def __init__(self, key):
        self.key = key
        self.events = {}        # seq -> (hash, where)
        self.order = []         # (seq, event, where) in input order
        self.problems = []
        self.signed = 0
        self.legacy_prev = {}   # hash of a pre-chain line -> where (the first chained event links to it)

    def bad(self, where, what):
        self.problems.append(f"{where}: {what}")

    def signature(self, where, data, sig):
        if self.key is None:
            self.bad(where, "signed, but no --key given to check it")
            return False
        if not sig_ok(self.key, data, sig):
            self.bad(where, "signature doesn't match (wrong key, or changed after signing)")
            return False
        self.signed += 1
        return True

    def event(self, where, raw):
        try:
            ev = json.loads(raw)
        except ValueError:
            self.bad(where, "not JSON")
            return
        if not isinstance(ev, dict):
            self.bad(where, "not an event")
            return
        if "seq" not in ev or "hash" not in ev:
            # A line written before the chain existed: the first chained event's prev is its SHA-256.
            self.legacy_prev[hashlib.sha256(raw.encode()).hexdigest()] = where
            return
        seq = ev.get("seq")
        if not isinstance(seq, int) or seq < 1:
            self.bad(where, f"bad seq {seq!r}")
            return
        if ev["hash"] != event_hash(ev):
            self.bad(where, f"seq {seq}: hash doesn't match the event (changed after it was written)")
        if seq in self.events:
            if self.events[seq][0] != ev["hash"]:
                self.bad(where, f"seq {seq}: a different event with the same seq is at {self.events[seq][1]}")
            return
        self.events[seq] = (ev["hash"], where)
        self.order.append((seq, ev, where))

    def local_line(self, where, line):
        self.event(where, line)

    def capture(self, where, rec):
        body, sig = rec.get("body"), rec.get("signature")
        if not isinstance(body, str):
            self.bad(where, "capture without a body")
            return
        self.signature(where, body.encode(), sig)
        try:
            doc = json.loads(body)
        except ValueError:
            self.bad(where, "body is not JSON")
            return
        if isinstance(doc, dict) and doc.get("specversion") == "1.0":
            data = doc.get("data")
            if not isinstance(data, dict):
                self.bad(where, "CloudEvent without event data")
                return
            raw = canonical(data)
            if doc.get("id") != (data.get("id") or data.get("hash")):
                self.bad(where, "CloudEvent id isn't the event's id")
            self.event(where, raw)
            return
        if isinstance(doc, dict) and "resourceLogs" in doc:
            n = 0
            for rl in doc.get("resourceLogs") or []:
                for sl in rl.get("scopeLogs") or []:
                    for lr in sl.get("logRecords") or []:
                        n += 1
                        w = f"{where} record {n}"
                        raw = ((lr.get("body") or {}).get("stringValue")) or ""
                        attrs = {a.get("key"): (a.get("value") or {}) for a in lr.get("attributes") or []}
                        rsig = (attrs.get("metacomputer.signature") or {}).get("stringValue")
                        self.signature(w, raw.encode(), rsig)
                        self.event(w, raw)
            if n == 0:
                self.bad(where, "OTLP request without log records")
            return
        self.bad(where, "body is neither a CloudEvent nor an OTLP logs request")

    SYSLOG = re.compile(r"^(?:\d+ )?<(\d{1,3})>1 (\S+) (\S+) (\S+) (\S+) (\S+) (\[.*?(?<!\\)\](?:\[.*?(?<!\\)\])*|-) (.*)$")

    def syslog(self, where, line):
        m = self.SYSLOG.match(line)
        if not m:
            self.bad(where, "not an RFC 5424 message")
            return
        sd, msg = m.group(7), m.group(8)
        if msg.startswith("﻿"):
            msg = msg[1:]
        params = {}
        for el in re.finditer(r"\[(\S+?)((?: \S+?=\"(?:[^\"\\]|\\.)*\")*)\]", sd):
            if el.group(1) == SD_ID:
                for p in re.finditer(r" (\S+?)=\"((?:[^\"\\]|\\.)*)\"", el.group(2)):
                    params[p.group(1)] = re.sub(r"\\(.)", r"\1", p.group(2))
        if not params:
            self.bad(where, f"no [{SD_ID}] element")
            return
        self.signature(where, msg.encode(), params.get("sig"))
        try:
            ev = json.loads(msg)
            if (str(ev.get("seq")) != params.get("seq") or ev.get("hash") != params.get("hash")
                    or ev.get("id", "") != params.get("id", "")):
                self.bad(where, "structured data doesn't match the event")
        except (ValueError, AttributeError):
            pass
        self.event(where, msg)

    def finish(self):
        if not self.order:
            self.bad("input", "no chained events found")
            return
        seqs = sorted(self.events)
        missing, prev_seq = [], None
        for s in seqs:
            if prev_seq is not None and s != prev_seq + 1:
                missing.append(f"{prev_seq + 1}" if s == prev_seq + 2 else f"{prev_seq + 1}-{s - 1}")
            prev_seq = s
        if missing:
            self.bad("chain", "missing seq " + ", ".join(missing))
        by_seq = {s: ev for s, ev, _ in self.order}
        for s, ev, where in self.order:
            p = ev.get("prev")
            if s - 1 in by_seq:
                if p != by_seq[s - 1]["hash"]:
                    self.bad(where, f"seq {s}: prev isn't seq {s - 1}'s hash (the chain is broken here)")
            elif s == 1 and p not in ("0" * 64, *self.legacy_prev):
                self.bad(where, "seq 1: prev is neither the zero hash nor an earlier line's hash")


def main(argv):
    ap = argparse.ArgumentParser(description="Check metacomputer secrets audit events (hashes, chain, signatures).")
    ap.add_argument("--key", help="the sink's signing key as printed (base64; hex also accepted), to check signatures")
    ap.add_argument("--key-file", help="a file holding that key")
    ap.add_argument("--quiet", action="store_true", help="print only problems")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    key = None
    try:
        if a.key_file:
            with open(a.key_file) as f:
                key = parse_key(f.read())
        elif a.key:
            key = parse_key(a.key)
    except (OSError, ValueError) as e:
        print(f"verify-audit: can't read the key: {e}", file=sys.stderr)
        return 2
    c = Checker(key)
    for path in a.files:
        try:
            f = sys.stdin if path == "-" else open(path, encoding="utf-8")
        except OSError as e:
            print(f"verify-audit: {e}", file=sys.stderr)
            return 2
        with f:
            for n, line in enumerate(f, 1):
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                where = f"{path}:{n}"
                stripped = re.sub(r"^\d+ (?=<)", "", line)
                if stripped.startswith("<"):
                    c.syslog(where, stripped)
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    c.bad(where, "neither JSON nor a syslog message")
                    continue
                if isinstance(rec, dict) and "body" in rec and "signature" in rec:
                    c.capture(where, rec)
                else:
                    c.local_line(where, line)
    c.finish()
    seqs = sorted(c.events)
    if c.problems:
        for p in c.problems:
            print("BAD  " + p)
        print(f"{len(c.problems)} problem(s) in {len(seqs)} event(s)")
        return 1
    if not a.quiet:
        sig = f", {c.signed} signature(s) verified" if c.signed else " (no signatures in the input)"
        print(f"OK   {len(seqs)} event(s), seq {seqs[0]}..{seqs[-1]}, hashes and chain intact{sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
