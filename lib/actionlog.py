#!/usr/bin/env python3
"""Signed action log (optional; ACTION_LOG=on, off by default).

Every connection to Cua Driver in the VM goes through `metacomputer mcp`
(the hub, which carries Claude Code's bridge, `metacomputer call` and scripts;
`web`; the tests). With the log on, that command runs mcp_relay.py, which
writes one JSON Lines file per connection under logs/actions/YYYY-MM-DD/: a
start record (who connected, from which project), then every tool call exactly
as it was sent to the VM (through the hub, with the client that made it), its
result summary and timing, and the calls the hub refused.
`metacomputer sh` (vm_shell.py) writes one file per run in the same form: a
`vm_shell` call (the script's SHA-256 and size, cwd, timeout) and its result
(exit code, and each output stream's size and SHA-256), unless SHELL_LOG=off.

- Each record carries the hash of the one before it ("prev") and its own
  ("hash" = SHA-256 of the record without "hash"), so editing, inserting or
  removing a record breaks the chain from that point on.
- Every 20 records, every 60 seconds with new records, and when the session
  ends, a checkpoint record signs the head of the chain with an SSH key
  (`ssh-keygen -Y sign`, namespace metacomputer-action-log). A checkpoint
  always signs the record right before it, and the final one is the file's
  last record; a checkpoint anywhere else (copied or replayed) fails
  verification. Records after the last checkpoint are reported as unsigned;
  a file without a final checkpoint ended abnormally or was cut short.
- Typed text, values and scripts are replaced by their length unless
  ACTION_LOG_TEXT=keep. Results are kept only as a keyed fingerprint (HMAC-SHA256 under a per-install secret beside the
  signing key, so short values in them can't be recovered by trying candidates) and sizes, all taken after card
  details are masked (lib/cardmask.py).

What it proves: that a file matches what the holder of the signing key
signed. It can't stop someone who has that key (anyone with your login on this
Mac, by default) from rewriting a log and signing it again; keep the key in
ssh-agent backed by hardware (ACTION_LOG_KEY=path/to/key.pub) to prevent that.

  actionlog.py list [--days N]
  actionlog.py show [FILE|latest] [--json]
  actionlog.py verify [FILE...|--all] [--key PUB]
  actionlog.py event KIND < JSON   one record in a file of its own (metadata only; used by
                                   `metacomputer secrets` for fills and sealed sessions)
"""
import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

NAMESPACE = "metacomputer-action-log"
SIGNER = "metacomputer"
FORMAT = 1
ZERO = "0" * 64
TEXT_KEYS = {"text", "value", "script", "password", "secret", "token"}
EVERY_N, EVERY_S = 20, 60
# A signer that doesn't answer (an agent or hardware key waiting for a touch or a
# passphrase) is stopped after this long, and that counts as a failed signature.
SIGN_TIMEOUT_S = float(os.environ.get("MCOMP_SIGN_TIMEOUT_S") or 10)

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import cardmask  # noqa: E402  (card details never reach the log: SECURITY.md, "Paying for things")
except Exception:  # without it, records that could hold a card's details aren't written in full
    cardmask = None
WITHHELD = "metacomputer couldn't check this record for card details, so its content isn't kept"


def _card_hints():
    """The masks for this connection's VM: the open purchases read fresh for every record (a purchase that opened a
    moment ago counts at once), and every one this relay has read before, for its life (cardmask.StickyHints)."""
    try:
        return cardmask.hints_for(os.environ)
    except Exception:
        return [{"exp": True, "cvc_len": 3}, {"cvc_len": 4}]


def mask_record(rec):
    """A record with card details masked (every key and value, the call's arguments, a result's text and an error's
    message alike) before it's written, the hash chain computed over that; one that can't be checked keeps only its
    type, id, tool and timing."""
    try:
        if cardmask is None:
            raise RuntimeError("no masker")
        cardmask.mask_value(rec, _card_hints())
        return rec
    except Exception:
        kept = {k: rec[k] for k in ("type", "id", "tool", "ms", "t", "ts") if k in rec}
        kept["withheld"] = WITHHELD
        return kept


def settings():
    """Resolved by bin/metacomputer (which sources the config) and passed in the environment."""
    ssh_dir = os.environ.get("MCOMP_SSH_DIR") or os.path.expanduser("~/.ssh/metacomputer")
    return {
        "on": os.environ.get("MCOMP_ACTION_LOG", "off") == "on",
        "keep_text": os.environ.get("MCOMP_ACTION_LOG_TEXT", "redact") == "keep",
        "dir": os.environ.get("MCOMP_ACTION_LOG_DIR") or os.path.join(os.environ.get("MCOMP_LOG_DIR") or os.path.join(LAB, "logs"), "actions"),
        "key": os.environ.get("MCOMP_ACTION_LOG_KEY") or os.path.join(ssh_dir, "action-log"),
    }


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def record_hash(rec):
    return hashlib.sha256(canonical({k: v for k, v in rec.items() if k != "hash"})).hexdigest()


def pub_path(key):
    return key if key.endswith(".pub") else key + ".pub"


def ensure_key(key):
    """Create the default signing key on first use (a .pub path means a key you manage, e.g. in ssh-agent)."""
    if key.endswith(".pub") or os.path.exists(key):
        return
    os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", NAMESPACE, "-f", key],
                   check=True, capture_output=True)


def fingerprint(pub):
    r = subprocess.run(["ssh-keygen", "-l", "-f", pub], capture_output=True, text=True)
    return (r.stdout.split() or ["", "?"])[1]


def checkpoint_message(name, seq, head, final):
    return f"{NAMESPACE}\nfile={name}\nseq={seq}\nhead={head}\nfinal={'true' if final else 'false'}\n".encode()


def redact(obj, depth=0):
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        return {k: ({"redacted": True, "chars": len(v)} if k in TEXT_KEYS and isinstance(v, str)
                    else redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, depth + 1) for v in obj]
    return obj


def result_mac_key(key):
    """A per-install secret beside the action log's signing key (0600, made on first use): results are fingerprinted with
    an HMAC under it, so a short value in a result (a card's security code, a postal code) can't be found from the log
    by trying every candidate, as an unkeyed hash would allow."""
    path = (os.path.join(os.path.dirname(key), "action-log.results-hmac") if key.endswith(".pub") else key + ".results-hmac")
    try:
        with open(path, "rb") as f:
            k = f.read(64).strip()
        if len(k) >= 32:
            return k
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    k = os.urandom(32).hex().encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def summarize_result(res, keep_text, mac_key=None):
    """A result's summary, from the result as masked (card details hidden): its error flag, a keyed fingerprint (HMAC-SHA256
    under mac_key; none without one), image count, text size and, with keep_text, its text."""
    if not isinstance(res, dict):
        return {"kind": type(res).__name__}
    images = sum(1 for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "image")
    texts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    out = {"error": bool(res.get("isError")), "images": images, "text_chars": sum(len(t) for t in texts)}
    if mac_key:
        out["hmac"] = hmac.new(mac_key, canonical(res), hashlib.sha256).hexdigest()
    if keep_text and texts:
        out["text"] = "\n".join(texts)[:400]
    return out


class ActionLog:
    """One session's log file. Thread-safe; every method is a no-op after a failure to sign."""

    def __init__(self, session, cfg=None, listener=None):
        cfg = cfg or settings()
        self.listener = listener    # called with (record, path) after each write: the optional event stream
        self.keep_text = cfg["keep_text"]
        self.key = cfg["key"]
        ensure_key(self.key)
        day = time.strftime("%Y-%m-%d")
        d = os.path.join(cfg["dir"], day)
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.name = f"{day}/{time.strftime('%H%M%S')}-{session.get('bridge_pid', os.getpid())}.jsonl"
        self.path = os.path.join(cfg["dir"], self.name)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.f = os.fdopen(fd, "a", buffering=1)
        self.lock = threading.Lock()
        self.head, self.seq, self.unsigned, self.last_sign = ZERO, 0, 0, time.time()
        self.closed = False
        self.sign_error = None
        self.started = {}
        self._append({"type": "start", "format": FORMAT, "file": self.name, "session": session,
                      "signing_key": fingerprint(pub_path(self.key)),
                      "text": "kept" if self.keep_text else "redacted"})
        self._timer = threading.Thread(target=self._tick, daemon=True)
        self._timer.start()

    # --- writing ------------------------------------------------------------
    def _append(self, rec):
        rec = {"seq": self.seq, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **rec, "prev": self.head}
        rec["hash"] = record_hash(rec)
        self.f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.head, self.seq = rec["hash"], self.seq + 1
        if self.listener:
            try:
                self.listener(rec, self.path)
            except Exception:
                pass
        return rec

    def _sign(self, final=False):
        if self.unsigned == 0 and not final:
            return
        covers = self.seq - 1
        msg = checkpoint_message(self.name, covers, self.head, final)
        try:   # run() kills and reaps a signer that times out
            r = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", self.key, "-n", NAMESPACE],
                               input=msg, capture_output=True, timeout=SIGN_TIMEOUT_S)
            err = None if r.returncode == 0 else (
                (r.stderr or b"").decode(errors="replace").strip()[:200] or "ssh-keygen failed")
        except subprocess.TimeoutExpired:
            err = f"ssh-keygen didn't finish within {SIGN_TIMEOUT_S:g} s"
        except OSError as e:
            err = f"couldn't run ssh-keygen ({e.strerror or e})"
        if err:
            self.sign_error = err   # sticky: sign_now() and close() report it from then on
            self._append({"type": "sign_failed", "covers_seq": covers, "error": self.sign_error})
            return
        self._append({"type": "checkpoint", "covers_seq": covers, "head": self.head,
                      "final": final, "sig": r.stdout.decode()})
        self.unsigned, self.last_sign = 0, time.time()

    def _add(self, rec):
        rec = mask_record(rec)
        with self.lock:
            if self.closed:
                return
            self._append(rec)
            self.unsigned += 1
            if self.unsigned >= EVERY_N:
                self._sign()

    def _tick(self):
        while not self.closed:
            time.sleep(5)
            with self.lock:
                if not self.closed and self.unsigned and time.time() - self.last_sign >= EVERY_S:
                    self._sign()

    def call(self, call_id, tool, args, client=None):
        """client: the hub's note on whose call it is (kind, pid, Claude Code pid, project, driver session)."""
        self.started[call_id] = time.time()
        rec = {"type": "call", "id": call_id, "tool": tool, "args": args if self.keep_text else redact(args)}
        if client:
            rec["client"] = client
        self._add(rec)

    def result(self, call_id, tool, msg):
        t0 = self.started.pop(call_id, None)
        rec = {"type": "result", "id": call_id, "tool": tool,
               "ms": int((time.time() - t0) * 1000) if t0 else None}
        # Masked first: what's cut short, fingerprinted or kept is the masked result, never the raw one.
        try:
            if cardmask is None:
                raise RuntimeError("no masker")
            msg = copy.deepcopy(msg)
            cardmask.mask_value(msg, _card_hints())
        except Exception:
            rec["withheld"] = WITHHELD
            self._add(rec)
            return
        if "error" in msg:
            # (masked again once cut short, whatever the text mode: an error's message is kept as text)
            rec["rpc_error"] = cardmask.mask_text(str((msg.get("error") or {}).get("message", ""))[:300], _card_hints())
        else:
            try:
                mk = result_mac_key(self.key)
            except OSError:
                mk = None
            rec["result"] = summarize_result(msg.get("result"), self.keep_text, mk)
        self._add(rec)

    def refused(self, tool, args, reason, client=None):
        rec = {"type": "refused", "tool": tool, "args": args if self.keep_text else redact(args), "reason": reason[:300]}
        if client:
            rec["client"] = client
        self._add(rec)

    def event(self, kind, **fields):
        self._add({"type": kind, **fields})

    def sign_now(self):
        """Sign the chain so far; True while every signature has worked."""
        with self.lock:
            if not self.closed:
                self._sign()
            return self.sign_error is None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self._append({"type": "end"})
            self.unsigned += 1
            self._sign(final=True)
            self.closed = True
            self.f.close()


# --- reading ------------------------------------------------------------------
def files(base, days=None):
    out = []
    if not os.path.isdir(base):
        return out
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400)) if days else ""
    for day in sorted(os.listdir(base)):
        if day < cutoff or not os.path.isdir(os.path.join(base, day)):
            continue
        for f in sorted(os.listdir(os.path.join(base, day)), key=_order):
            if f.endswith(".jsonl"):
                out.append(os.path.join(base, day, f))
    return out


def _order(name):
    """Files are TIME-PID.jsonl: order by time, then by the PID as a number, so a
    5-digit PID started in the same second as a 4-digit one sorts after it."""
    stem = name[:-6] if name.endswith(".jsonl") else name
    t, _, pid = stem.partition("-")
    return (t, 0, int(pid), name) if pid.isdigit() else (t, 1, 0, name)


def load(path):
    recs, bad = [], []
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            bad.append(i)
    return recs, bad


def verify_sig(pub, message, sig):
    with tempfile.TemporaryDirectory() as td:
        allowed = os.path.join(td, "allowed")
        with open(allowed, "w") as f:
            f.write(f'{SIGNER} namespaces="{NAMESPACE}" {open(pub).read().strip()}\n')
        sigf = os.path.join(td, "sig")
        with open(sigf, "w") as f:
            f.write(sig)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", allowed, "-I", SIGNER, "-n", NAMESPACE, "-s", sigf],
                           input=message, capture_output=True)
        return r.returncode == 0


def verify_file(path, base, pub):
    """Returns a dict: ok (no tampering found), problems, signed_through, unsigned, final, calls."""
    recs, bad = load(path)
    # Signatures name the file as it was written (DAY/TIME-PID.jsonl), so a copy
    # (e.g. in a task folder) still verifies; the chain ties the name to the rest.
    name = (recs[0].get("file") if recs and recs[0].get("type") == "start" else None) or os.path.relpath(path, base)
    problems = [f"line {n}: not JSON" for n in bad]
    prev, signed_through, final, calls = ZERO, -1, False, 0
    for i, r in enumerate(recs):
        seq = r.get("seq")
        if seq != i:
            problems.append(f"record {i}: sequence number {seq} (records missing or reordered)")
        if r.get("prev") != prev:
            problems.append(f"record {i}: chain broken (doesn't follow the record before it)")
        if r.get("hash") != record_hash(r):
            problems.append(f"record {i}: contents changed after it was written")
        if r.get("type") == "call":
            calls += 1
        if r.get("type") == "checkpoint":
            cs = r.get("covers_seq")
            covered = recs[cs] if isinstance(cs, int) and 0 <= cs < i else None
            msg = checkpoint_message(name, cs, r.get("head"), bool(r.get("final")))
            # A checkpoint signs the record right before it, as the writer always has: a copy of
            # an older checkpoint (say the final one, appended after forged records) names a record
            # further back and fails here, though its signature would still verify.
            if cs != i - 1 or r.get("head") != r.get("prev"):
                problems.append(f"record {i}: checkpoint doesn't sign the record right before it "
                                f"(covers {cs}; a copied or moved checkpoint)")
            elif covered is None or covered.get("hash") != r.get("head"):
                problems.append(f"record {i}: checkpoint doesn't match record {cs}")
            elif not verify_sig(pub, msg, r.get("sig", "")):
                problems.append(f"record {i}: signature doesn't verify with {os.path.basename(pub)}")
            elif r.get("final") and i != len(recs) - 1:
                # The final checkpoint ends the file: the writer closes it right after.
                problems.append(f"record {i}: records after the final checkpoint (added after the log was closed)")
            else:
                signed_through = cs
                final = final or bool(r.get("final"))
        prev = r.get("hash")
    last_cp = max((i for i, r in enumerate(recs) if r.get("type") == "checkpoint"), default=-1)
    unsigned = len(recs) - 1 - last_cp if recs else 0
    start = recs[0] if recs and recs[0].get("type") == "start" else {}
    return {"file": name, "ok": not problems, "problems": problems, "records": len(recs), "calls": calls,
            "signed_through": signed_through, "unsigned": unsigned, "final": final,
            "project": (start.get("session") or {}).get("cwd"), "signing_key": start.get("signing_key")}


# What `event` keeps from its input: the result `mcomp-vault fill|session` prints, which
# never holds a value. Anything else is dropped.
EVENT_KEYS = ("ok", "name", "vm", "field", "profile", "origin", "chars", "bytes", "route", "reason", "token_id", "api_host", "expires_in_s")


def record_event(kind, data, cfg=None):
    """One event in a signed file of its own; returns the file's path (None when the log is off)."""
    cfg = cfg or settings()
    if not cfg["on"]:
        return None
    fields = {}
    for k in EVENT_KEYS:
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, (bool, int)) or (isinstance(v, str) and len(v) <= 300):
            fields[k] = v
    log = ActionLog({"bridge_pid": os.getpid(), "cwd": os.getcwd(), "via": kind,
                     "vm": os.environ.get("MCOMP_VM_NAME", "")}, cfg)
    log.event(kind, **fields)
    log.close()
    return log.path


def _fmt_args(a):
    s = json.dumps(a, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= 110 else s[:107] + "..."


def main(argv):
    cfg = settings()
    base = cfg["dir"]
    cmd = argv[0] if argv else "list"
    rest = argv[1:]
    if cmd == "list":
        days = int(rest[rest.index("--days") + 1]) if "--days" in rest else 7
        fs = files(base, days)
        if not fs:
            print(f"no action logs in the last {days} days ({base})"
                  + ("" if cfg["on"] else "; the log is off: set ACTION_LOG=on in config/local.env"))
            return 0
        for p in fs:
            recs, _ = load(p)
            start = recs[0] if recs else {}
            calls = sum(1 for r in recs if r.get("type") == "call")
            errs = sum(1 for r in recs if r.get("type") == "result" and (r.get("rpc_error") or (r.get("result") or {}).get("error")))
            refused = sum(1 for r in recs if r.get("type") == "refused")
            ended = any(r.get("type") == "end" for r in recs)
            print(f"{os.path.relpath(p, base):32} {calls:4} calls {errs:3} errors {refused:2} refused  "
                  f"{'ended ' if ended else 'open  '} {(start.get('session') or {}).get('cwd', '')}")
        return 0
    if cmd == "show":
        js = "--json" in rest
        target = next((a for a in rest if not a.startswith("--")), "latest")
        fs = files(base)
        path = (fs[-1] if fs else None) if target == "latest" else (
            target if os.path.exists(target) else os.path.join(base, target))
        if not path or not os.path.exists(path):
            print("no such action log", file=sys.stderr)
            return 1
        recs, _ = load(path)
        if js:
            print(json.dumps(recs, indent=1, ensure_ascii=False))
            return 0
        for r in recs:
            t, ts = r.get("type"), (r.get("ts") or "")[11:19]
            if t == "call":
                print(f"{r['seq']:5} {ts} -> {r.get('tool')} {_fmt_args(r.get('args'))}")
            elif t == "result" and r.get("tool") == "vm_shell":   # metacomputer sh (lib/vm_shell.py)
                res = r.get("result") or {}
                state = f"exit {res.get('exit_code')}" if res.get("status") == "exited" else str(res.get("status"))
                sizes = ", ".join(f"{k} {(res.get(k) or {}).get('bytes', 0)} B" for k in ("stdout", "stderr") if k in res)
                print(f"{r['seq']:5} {ts} <- vm_shell {state} ({r.get('ms')} ms" + (f"; {sizes})" if sizes else ")"))
            elif t == "result":
                res = r.get("result") or {}
                state = f"rpc error: {r['rpc_error']}" if r.get("rpc_error") else ("ERROR" if res.get("error") else "ok")
                print(f"{r['seq']:5} {ts} <- {r.get('tool')} {state} ({r.get('ms')} ms, {res.get('images', 0)} img)")
            elif t == "refused":
                print(f"{r['seq']:5} {ts} xx {r.get('tool')} refused: {r.get('reason', '')[:90]}")
            elif t == "checkpoint":
                print(f"{r['seq']:5} {ts} == signed through record {r.get('covers_seq')}" + (" (final)" if r.get("final") else ""))
            else:
                extra = {k: v for k, v in r.items() if k not in ("seq", "ts", "type", "prev", "hash")}
                print(f"{r['seq']:5} {ts} {t} {_fmt_args(extra) if extra else ''}")
        return 0
    if cmd == "verify":
        pub = pub_path(rest[rest.index("--key") + 1]) if "--key" in rest else pub_path(cfg["key"])
        if not os.path.exists(pub):
            print(f"no public key at {pub} (pass --key PUB)", file=sys.stderr)
            return 2
        targets = [a for i, a in enumerate(rest) if not a.startswith("--") and (i == 0 or rest[i - 1] != "--key")]
        paths = files(base) if "--all" in rest or not targets else [
            t if os.path.exists(t) else os.path.join(base, t) for t in targets]
        if not targets and "--all" not in rest:
            paths = paths[-1:]
        if not paths:
            print("no action logs to verify")
            return 0
        bad, failed = 0, []
        for p in paths:
            v = verify_file(p, base, pub)
            if not v["ok"]:
                bad += 1
                failed.append(v["file"])
                print(f"TAMPERED  {v['file']}")
                for pr in v["problems"][:10]:
                    print(f"          {pr}")
            else:
                notes = []
                if v["unsigned"]:
                    notes.append(f"{v['unsigned']} record(s) after the last signature")
                if not v["final"]:
                    notes.append("no final signature (session still open, crashed, or cut short)")
                print(f"{'OK' if not notes else 'OK*':9} {v['file']}  {v['calls']} calls, signed through record "
                      f"{v['signed_through']}" + (f"; {'; '.join(notes)}" if notes else ""))
        if bad:
            try:   # the optional event stream (docs/events.md); never changes the result
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import events
                events.emit("security", "actions_verify_failed",
                            f"metacomputer actions verify: {bad} of {len(paths)} action log(s) failed verification",
                            "error", "Keep copies of the files and run metacomputer actions verify --all to see what changed",
                            {"failed": bad, "checked": len(paths), "files": failed[:10]}, project=events.caller_project())
            except Exception:
                pass
        return 1 if bad else 0
    if cmd == "event":
        kind = rest[0] if rest else ""
        if not kind.replace("_", "").isalpha() or len(kind) > 40:
            print("usage: actionlog.py event KIND < JSON", file=sys.stderr)
            return 2
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        record_event(kind, data, cfg)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
