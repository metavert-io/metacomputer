#!/usr/bin/env python3
"""verify-audit.py: check metacomputer secrets' audit events, in the local log or as a sink got them.

    verify-audit.py [--expect-start SEQ] [--expect-head SEQ:HASH] audit.jsonl          (local mode)
    verify-audit.py --key KEY [--require-verified] [--expect-start SEQ] [--expect-head SEQ:HASH] CAPTURE...
                                                                                     (capture mode)

Local mode (no --key) reads the vault's own log, audit.jsonl: one event per line, in order. It
checks each event's hash (SHA-256 of its canonical JSON without "hash"), the chain (each "prev"
is the previous event's hash) and that seq goes up by one, line by line, with no repeats. A
signed capture is refused here (give --key). This proves the file is self-consistent; it can't
prove that nobody rewrote the whole file, since your account can recompute every hash. Only the
unlock process can check each record's creation MAC: `metacomputer secrets audit --verify`.

Capture mode (--key, the sink's key as `audit-sink add` printed it: 32 bytes in base64, hex also
accepted; the HMAC uses the decoded bytes) reads what a sink received. Every event must come
signed: a raw event line is refused. Accepted inputs, detected line by line:
  - webhook: JSON lines {"signature": "<X-Metacomputer-Signature>", "body": "<request body>"},
    each body a CloudEvent whose `data` is the event and `mcompverified` its producer check;
  - OTLP: the same {"signature", "body"} lines, each body an OTLP/HTTP JSON logs request; every
    log record also carries its own metacomputer.signature and metacomputer.verified;
  - syslog: one RFC 5424 message per line (octet count removed), the event as MSG, with
    sig= and verified= in the [metacomputer@32473 ...] element.
Repeated deliveries of the same event (same seq, same hash) are fine (delivery is at least
once); otherwise each new seq must follow the one before, in the order received. An
`audit_tamper` notice (the vault found a record it wouldn't sign) fails the check. Events marked
unverified were written without the vault key (refusals while the vault was locked, or records
from before the vault had an audit key); they're counted, and --require-verified fails on them.
The status counts only when it's authenticated: a webhook's comes in its signed body, an OTLP
record's and a syslog message's signature covers "verified=true|false" and the line together.
An absent status (an older capture) counts as unverified.

--expect-start SEQ and --expect-head SEQ:HASH (from `metacomputer secrets audit --head`, noted at a
moment you trust, or from another sink) pin the ends; without them a check can't tell whether
events are missing before the first or after the last one given.

The ntfy sink's summaries carry no signature and aren't accepted. Exit 0 when everything checks
out, 1 when something doesn't, 2 for usage errors. Standard library only.
"""
import argparse, base64, binascii, hashlib, hmac, json, re, sys

ZERO = "0" * 64


sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))), "lib"))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)
import sinkkit.registry  # noqa: E402
SD_ID = sinkkit.registry.data("syslog")["format"]["sd_id"]     # metacomputer@32473 (sinks/syslog/manifest.json)


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


SYSLOG = re.compile(r"^<(\d{1,3})>1 (\S+) (\S+) (\S+) (\S+) (\S+) (\[.*?(?<!\\)\](?:\[.*?(?<!\\)\])*|-) (.*)$")


class Checker:
    def __init__(self, key, require_verified):
        self.key = key
        self.capture = key is not None
        self.require_verified = require_verified
        self.seen = {}           # seq -> (hash, where)
        self.order = []          # (seq, event, where) for each new seq, in order
        self.problems = []
        self.signed = 0
        self.unverified = []     # seqs marked unverified by the sink's wrapping
        self.legacy = set()      # hashes of pre-chain lines (local mode)
        self.dups = 0

    def bad(self, where, what):
        self.problems.append(f"{where}: {what}")

    def signature(self, where, data, sig):
        if not sig_ok(self.key, data, sig):
            self.bad(where, "signature doesn't match (wrong key, or changed after signing)")
            return False
        self.signed += 1
        return True

    # one event, from either mode
    def event(self, where, raw, verified=None):
        try:
            ev = json.loads(raw)
        except ValueError:
            self.bad(where, "not JSON")
            return
        if not isinstance(ev, dict):
            self.bad(where, "not an event")
            return
        if ev.get("op") == "audit_tamper" and "hash" not in ev:
            self.bad(where, f"tamper notice from the vault for seq {ev.get('seq')}: {ev.get('reason')}")
            return
        if "seq" not in ev or "hash" not in ev:
            if self.capture:
                self.bad(where, "an event without seq and hash")
            elif self.order:
                self.bad(where, "a line without seq and hash after the chain began")
            else:
                self.legacy.add(hashlib.sha256(raw.encode()).hexdigest())
            return
        seq = ev.get("seq")
        if not isinstance(seq, int) or seq < 1:
            self.bad(where, f"bad seq {seq!r}")
            return
        if ev["hash"] != event_hash(ev):
            self.bad(where, f"seq {seq}: hash doesn't match the event (changed after it was written)")
        if seq in self.seen:
            if self.capture and self.seen[seq][0] == ev["hash"]:
                self.dups += 1
                return
            self.bad(where, f"seq {seq} again (first at {self.seen[seq][1]})" + ("" if self.capture else ": a local log has each seq once"))
            return
        if self.order and seq != self.order[-1][0] + 1:
            self.bad(where, f"seq {seq} after seq {self.order[-1][0]}: out of order, or seq {self.order[-1][0] + 1}"
                            + (f"..{seq - 1}" if seq - 1 > self.order[-1][0] + 1 else "") + " missing")
        if self.order and self.order[-1][0] == seq - 1 and ev.get("prev") != self.order[-1][1]["hash"]:
            self.bad(where, f"seq {seq}: prev isn't seq {seq - 1}'s hash (the chain is broken here)")
        if not self.order and seq == 1 and ev.get("prev") not in (ZERO, *self.legacy):
            self.bad(where, "seq 1: prev is neither the zero hash nor an earlier line's hash")
        if self.capture and verified is not True:
            # Only an authenticated true counts: false, absent (an older capture) or malformed is unverified.
            self.unverified.append(seq)
        self.seen[seq] = (ev["hash"], where)
        self.order.append((seq, ev, where))

    def local_line(self, where, line):
        if self.capture:
            self.bad(where, "an unsigned event: with --key every event must come signed from a sink")
            return
        self.event(where, line)

    def wrapper(self, where, rec):
        if not self.capture:
            self.bad(where, "a signed capture: give the sink's --key")
            return
        body, sig = rec.get("body"), rec.get("signature")
        if not isinstance(body, str):
            self.bad(where, "capture without a body")
            return
        if not self.signature(where, body.encode(), sig):
            return
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
            if doc.get("id") != (data.get("id") or data.get("hash")):
                self.bad(where, "CloudEvent id isn't the event's id")
            v = doc.get("mcompverified")
            self.event(where, canonical(data), v if isinstance(v, bool) else None)
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
                        vb = (attrs.get("metacomputer.verified") or {}).get("boolValue")
                        vb = vb if isinstance(vb, bool) else None
                        # The record's own signature covers its verification status and its line.
                        signed = (f"verified={'true' if vb else 'false'}\n" + raw) if vb is not None else raw
                        if not self.signature(w, signed.encode(), (attrs.get("metacomputer.signature") or {}).get("stringValue")):
                            continue
                        self.event(w, raw, vb)
            if n == 0:
                self.bad(where, "OTLP request without log records")
            return
        self.bad(where, "body is neither a CloudEvent nor an OTLP logs request")

    def syslog(self, where, line):
        if not self.capture:
            self.bad(where, "a signed syslog capture: give the sink's --key")
            return
        m = SYSLOG.match(line)
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
        v = params.get("verified")
        v = v if v in ("true", "false") else None
        # sig covers the verification status and MSG: flipping verified= breaks it.
        signed = (f"verified={v}\n" + msg) if v is not None else msg
        if not self.signature(where, signed.encode(), params.get("sig")):
            return
        try:
            ev = json.loads(msg)
            if ev.get("op") != "audit_tamper" and (str(ev.get("seq")) != params.get("seq") or ev.get("hash") != params.get("hash")
                                                   or ev.get("id", "") != params.get("id", "")):
                self.bad(where, "structured data doesn't match the event")
        except (ValueError, AttributeError):
            pass
        self.event(where, msg, None if v is None else v == "true")

    def finish(self, start, head):
        if not self.order:
            self.bad("input", "no chained events found")
            return
        first, last = self.order[0], self.order[-1]
        if start is not None and first[0] != start:
            self.bad("input", f"the first event is seq {first[0]}, not the expected {start}")
        if head is not None:
            hs, hh = head
            if last[0] != hs or last[1].get("hash") != hh:
                self.bad("input", f"the last event is {last[0]}:{last[1].get('hash')}, not the expected head {hs}:{hh}")
        if self.require_verified and self.unverified:
            self.bad("input", f"{len(self.unverified)} event(s) not producer-verified: seq {', '.join(map(str, self.unverified[:20]))}")


def main(argv):
    ap = strictargs.Parser(description="Check metacomputer secrets audit events (hashes, chain, order, signatures).")
    ap.add_argument("--key", help="capture mode: the sink's signing key as printed (base64; hex also accepted)")
    ap.add_argument("--key-file", help="capture mode: a file holding that key")
    ap.add_argument("--expect-start", type=int, help="the first event must have this seq")
    ap.add_argument("--expect-head", help="SEQ:HASH the last event must be")
    ap.add_argument("--require-verified", action="store_true", help="capture mode: fail on events the vault marked unverified")
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
    head = None
    if a.expect_head:
        m = re.fullmatch(r"(\d+):([0-9a-f]{64})", a.expect_head.strip())
        if not m:
            print("verify-audit: --expect-head SEQ:HASH (64 hex digits)", file=sys.stderr)
            return 2
        head = (int(m.group(1)), m.group(2))
    if a.require_verified and key is None:
        print("verify-audit: --require-verified goes with --key (capture mode)", file=sys.stderr)
        return 2
    c = Checker(key, a.require_verified)
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
                    c.wrapper(where, rec)
                else:
                    c.local_line(where, line)
    c.finish(a.expect_start, head)
    seqs = [s for s, _, _ in c.order]
    if c.problems:
        for p in c.problems:
            print("BAD  " + p)
        print(f"{len(c.problems)} problem(s) in {len(seqs)} event(s)")
        return 1
    if not a.quiet:
        mode = "capture" if c.capture else "local log"
        print(f"OK   {mode}: {len(seqs)} event(s), seq {seqs[0]}..{seqs[-1]}"
              + (f", {c.dups} repeated delivery(ies) dropped" if c.dups else ""))
        proves = ["each event's hash and its link to the one before", "no gaps or reordering between them"]
        if c.capture:
            proves.append(f"every event signed with this sink's key ({c.signed} signature(s))")
            proves.append(f"{len(seqs) - len(c.unverified)} event(s) producer-verified by the vault"
                          + (f"; {len(c.unverified)} written without the vault key (seq {', '.join(map(str, c.unverified[:10]))})" if c.unverified else ""))
        if a.expect_start is not None:
            proves.append(f"it starts at seq {a.expect_start}")
        if head:
            proves.append(f"it ends at the expected head {head[0]}")
        print("     Proves: " + "; ".join(proves) + ".")
        not_proven = []
        if a.expect_start is None:
            not_proven.append("that nothing came before the first event (--expect-start)")
        if not head:
            not_proven.append("that nothing came after the last one (--expect-head)")
        if not c.capture:
            not_proven.append("that the file wasn't rewritten whole (a sink's capture, or `metacomputer secrets audit --verify`, can)")
        if not_proven:
            print("     Doesn't prove: " + "; ".join(not_proven) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
