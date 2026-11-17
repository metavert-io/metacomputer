#!/usr/bin/env python3
"""Optional event stream: `metacomputer events` and `metacomputer notify` (docs/events.md).

Tells people running long or unattended sessions when something needs them or
matters, through sinks they choose. Off by default: with no sink configured
nothing leaves this Mac, and every command behaves as before. Every event is also
appended to this VM's local log ($LOG_DIR/events.jsonl).

  events.py sink add NAME --kind webhook|otlp|syslog|ntfy --url URL [--header 'Name: value' ...]
                 [--header-env 'Name=VAR' ...] [--ca FILE] [--families F1,F2|all] [--min-severity info|warn|error]
                 [--replace] [--yes]                 (add and remove: only from a terminal; announced to the sinks)
  events.py sink list [--json] | show NAME [--key] [--json] | test NAME | remove NAME [--yes]
  events.py tail [N] [--json]                        the local event log
  events.py emit FAMILY TYPE --message TEXT [--severity S] [--action TEXT] [--data JSON] [--debounce S] [--quiet]
  events.py notify TEXT [--why login|captcha|2fa|dialog|approval|other] [--action TEXT]
  events.py status [--json|--line]
  events.py drain [--now]                            the spool, then every sink's backlog
  events.py watch [--once [--health]]                the watcher (`metacomputer start` runs it)
  events.py has-sinks                                exit 0 when a sink is configured

Kinds (the same formats as the secrets vault's audit sinks):
  webhook  CloudEvents 1.0 structured JSON, signed: X-Metacomputer-Signature: sha256=HMAC(body)
  otlp     OpenTelemetry logs, OTLP/HTTP JSON (POST URL/v1/logs)
  syslog   RFC 5424 over TLS with octet counting (RFC 5425), syslog+tls://host:port
  ntfy     one short human message per event (https; http only to this Mac's loopback)

Delivery is best effort and at least once: synchronous with a 3 s timeout per sink
at the point of emission, then an on-disk backlog per sink retried with backoff by
later emissions and by the watcher, in order. A failing sink never fails the
command that emitted. At most RATE_CAP events per family per RATE_WINDOW reach the
sinks (the rest are logged and summed up). Every event says which code path made it
(`origin`). Stdlib only.
"""
import argparse
import base64
import calendar
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAMILIES = ("needs_you", "task", "security", "lifecycle", "actions")
ALL = ("needs_you", "task", "security", "lifecycle")          # "all"; actions is opt-in only
DEFAULT_SEVERITY = {"needs_you": "warn", "task": "info", "security": "warn", "lifecycle": "info", "actions": "info"}
SEVERITIES = ("info", "warn", "error")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
KINDS = ("webhook", "otlp", "syslog", "ntfy")
WHY = ("login", "captcha", "2fa", "dialog", "approval", "other")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")

ORIGINS = ("cli", "bridge", "watcher", "spool")   # which code path made an event (set by the code, not the caller)

TIMEOUT = 3.0           # per request
EMIT_BUDGET = 3.0       # per sink, at the point of emission
WATCH_BUDGET = 20.0     # per sink, per watcher cycle
WATCH_INTERVAL = 30
HEALTH_INTERVAL = 300
HEALTH_RENOTIFY_S = 1800  # a health condition that comes back within 30 min isn't announced again
NETGW_BATCH_S = 60        # at most one blocked-connections event a minute ...
NETGW_MAX_S = 3600        # ... and, for the same destinations again and again, doubling up to once an hour
DEBOUNCE_S = 300
EMIT_DUP_S = 60           # `events emit`: the same event again within a minute is dropped
RATE_CAP, RATE_WINDOW = 10, 600   # per family: at most 10 events per 10 minutes reach the sinks
RATE_EXEMPT = {("lifecycle", "test"), ("security", "sink_added"), ("security", "sink_removed")}
MAX_BACKLOG = 10000
LOG_MAX = 10 * 2**20
SPOOL_MAX_BYTES = 64 * 1024
BATCH = {"webhook": 1, "ntfy": 1, "otlp": 50, "syslog": 100}

CE_PREFIX = "io.metavert.metacomputer"
SIGNATURE_HEADER = "X-Metacomputer-Signature"
SD_ID = "metacomputer@32473"     # 32473: the documentation enterprise number (RFC 5612)
SYSLOG_FACILITY = 1              # user-level
SYSLOG_SEV = {"info": 6, "warn": 4, "error": 3}
OTLP_SEV = {"info": (9, "INFO"), "warn": (13, "WARN"), "error": (17, "ERROR")}
USER_AGENT = "metacomputer-events/1"


class EventsError(Exception):
    pass


class Busy(Exception):
    pass


# --- where things live ----------------------------------------------------------
def events_dir():
    return os.environ.get("MCOMP_EVENTS_DIR") or os.path.expanduser("~/.config/metacomputer")


def config_path():
    return os.path.join(events_dir(), "events.json")


def state_dir():
    return os.path.join(events_dir(), "events-state")


def spool_dir():
    return os.environ.get("MCOMP_EVENTS_SPOOL") or os.path.expanduser(
        "~/Library/Application Support/metacomputer/events/spool")


def log_dir():
    return os.environ.get("MCOMP_LOG_DIR") or os.path.join(LAB, "logs")


def log_path():
    return os.path.join(log_dir(), "events.jsonl")


def run_dir():
    return os.environ.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run")


def watcher_pidfile():
    return os.path.join(run_dir(), "events-watcher.pid")


def sink_file(name, ext):
    return os.path.join(state_dir(), "sinks", f"{name}.{ext}")


def vm_name():
    if os.environ.get("MCOMP_VM_NAME"):
        return os.environ["MCOMP_VM_NAME"]
    try:
        import labconfig
        return labconfig.CONFIG.get("VM_NAME") or "metacomputer"
    except Exception:
        return "metacomputer"


def host_name():
    return (socket.gethostname() or "localhost").split(".")[0] or "localhost"


def private_dir(path):
    """Create path (and missing parents) 0700; refuse a link or a folder owned by someone else."""
    missing = []
    p = path
    while p and not os.path.exists(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise EventsError(f"unsafe folder {path} (a link, or not yours)")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private(path, text):
    private_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def append_private(path, data):
    private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Lock:
    """An exclusive flock on a file in the state folder, waiting up to `wait` seconds."""

    def __init__(self, path, wait=10.0):
        self.path, self.wait, self.fd = path, wait, None

    def __enter__(self):
        private_dir(os.path.dirname(self.path))
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= end:
                    os.close(self.fd)
                    self.fd = None
                    raise Busy(self.path)
                time.sleep(0.02)

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def global_lock(wait=10.0):
    return Lock(os.path.join(state_dir(), "lock"), wait)


def note_error(err):
    """Problems in the event code itself go to a small file `events status` shows, never to the caller."""
    try:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [{os.getpid()}] {str(err)[:300]}\n"
        p = os.path.join(state_dir(), "errors.log")
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            os.replace(p, p + ".1")
        append_private(p, line.encode())
    except Exception:
        pass


# --- configuration ----------------------------------------------------------------
def load_config():
    p = config_path()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return {"version": 1, "sinks": {}}
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise EventsError(f"{p} is a link or not yours; not reading it")
    if st.st_mode & 0o077:
        os.chmod(p, 0o600)
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("sinks", {}), dict):
        raise EventsError(f"{p}: not an events configuration")
    cfg.setdefault("version", 1)
    cfg.setdefault("sinks", {})
    return cfg


def save_config(cfg):
    private_dir(events_dir())
    write_private(config_path(), json.dumps(cfg, indent=2, sort_keys=True) + "\n")


def sink_families(sink):
    fams = set()
    for f in sink.get("families") or ["all"]:
        fams |= set(ALL) if f == "all" else {f}
    return fams


def matches(sink, ev):
    return (ev["family"] in sink_families(sink)
            and SEV_RANK.get(ev["severity"], 0) >= SEV_RANK.get(sink.get("min_severity") or "info", 0))


def wants(cfg, family):
    return any(family in sink_families(s) for s in cfg.get("sinks", {}).values())


# --- cleaning -----------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x9b[0-?]*[ -/]*[@-~]")
# Control characters, soft hyphen, zero-width and bidirectional controls, line and paragraph
# separators, invisible operators, the BOM and tag characters.
CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f­؜​-‏ -‮⁠-⁤⁦-⁩﻿"
                     "\U000e0000-\U000e007f]")
SURROGATE_RE = re.compile("[\ud800-\udfff]")
# Where a link starts inside a word: scheme://, //host, a scheme without slashes (mailto:, data:, ...),
# or host.name followed by / ? # or ;.
URL_START_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{1,15}://"
    r"|(?<![:/\w])//(?=[\w\[])"
    r"|(?<![A-Za-z0-9+.-])(?:mailto|tel|sms|data|javascript|about|blob|file|news|urn|magnet|intent):"
    r"|(?<![\w@.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z][A-Za-z0-9-]*(?::\d+)?(?=[/?#;])", re.I)
QUERY_LIKE_RE = re.compile(r"\?[^?]*[=&]")
SECRET_KEY_RE = re.compile(
    r"pass(word|wd|phrase|code)?s?$|passcode|pwd|secret|token|cookie|header|credential|api.?key|private.?key|"
    r"session|jwt|bearer|(^|_)key$|(^|_)sig(nature)?($|_)|(^|_)auth(orization)?($|_)|_code$|"
    r"^(text|value|values|script|typed|otp|totp|code|pin|vnc.*)$", re.I)
MAX = {"message": 500, "action": 300, "string": 300, "list": 50, "keys": 50, "depth": 4, "data_bytes": 4096}
_known = {"at": 0.0, "values": [], "tags": []}


def url_secret_parts(url):
    """The parts of a sink's URL that may be secret: path segments mask_url hides, query values."""
    try:
        u = urlsplit(url)
    except ValueError:
        return []
    parts = [p for p in u.path.split("/") if p and p.lower() not in KEEP_SEGMENTS]
    for kv in u.query.split("&"):
        parts.append(kv.partition("=")[2])
    return [p for p in parts if len(p) >= 6]


def known_secrets():
    """Values that must never appear in an event: sink header values, signing keys and secret URL parts,
    the live-view password (from its file), and remembered values matched by a short keyed tag."""
    if time.time() - _known["at"] < 30:
        return _known["values"]
    vals = []
    try:
        for s in load_config().get("sinks", {}).values():
            for _, v in s.get("headers") or []:
                vals.append(v)
                vals.extend(p for p in str(v).split() if len(p) >= 8)    # the token in "Bearer <token>"
            if s.get("hmac_key"):
                vals.append(s["hmac_key"])
            vals.extend(url_secret_parts(s.get("url") or ""))
    except Exception:
        pass
    try:
        p = os.path.join(os.environ.get("MCOMP_SSH_DIR") or os.path.expanduser("~/.ssh/metacomputer"), "vnc-pass")
        if os.path.getsize(p) < 256:
            vals.append(open(p).read().strip())
    except OSError:
        pass
    _known["values"] = sorted({v for v in vals if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)
    tags = read_json(os.path.join(state_dir(), "redact.json"), {})
    _known["tags"] = [t for t in tags.values() if isinstance(t, dict) and isinstance(t.get("len"), int)
                      and 6 <= t["len"] <= 64] if isinstance(tags, dict) else []
    _known["at"] = time.time()
    return _known["values"]


def remember_redaction(label, value):
    """Remember a value to redact without storing it: its length, a random key and the first 3 bytes of its
    HMAC. Too short to recover the value from, so only a rare innocent match is redacted by mistake."""
    key = secrets.token_bytes(16)
    tag = hmac.new(key, value.encode(), hashlib.sha256).digest()[:3].hex()
    p = os.path.join(state_dir(), "redact.json")
    with global_lock():
        tags = read_json(p, {})
        tags = tags if isinstance(tags, dict) else {}
        tags[label] = {"len": len(value), "key": key.hex(), "tag": tag}
        write_private(p, json.dumps(tags))
    _known["at"] = 0


def redact_tagged(s):
    for t in _known["tags"]:
        n, key = t["len"], bytes.fromhex(t["key"])
        if len(s) < n:
            continue
        i = 0
        while i + n <= len(s):
            w = s[i:i + n]
            if not w.isspace() and hmac.new(key, w.encode(), hashlib.sha256).digest()[:3].hex() == t["tag"]:
                s = s[:i] + "[redacted]" + s[i + n:]
                i += len("[redacted]")
            else:
                i += 1
    return s


def clean_token(tok):
    """One word, with any link in it cut down: no user:password@, no ;params, nothing from ? or # on.
    Returns (word, cut a query)."""
    m = URL_START_RE.search(tok)
    if not m:
        q = QUERY_LIKE_RE.search(tok)                 # "p?token=x" without a recognisable host
        return (tok[:q.start()], True) if q else (tok, False)
    head, url = tok[:m.start()], tok[m.start():]
    lead = m.group(0)
    rest = url[len(lead):]
    if lead.endswith("//"):                           # an authority: drop credentials
        end = len(rest)
        for c in "/?#;":
            j = rest.find(c)
            if j >= 0:
                end = min(end, j)
        rest = rest[:end].rsplit("@", 1)[-1] + rest[end:]
    cut = min([i for i in (rest.find("?"), rest.find("#")) if i >= 0], default=-1)
    query = cut >= 0 and rest[cut] == "?"
    if cut >= 0:
        rest = rest[:cut]
    rest = re.sub(r";[^/]*", "", rest)                # path parameters (;jsessionid=...)
    return head + lead + rest, query


def clean_urls(s):
    """Links lose credentials, path parameters, the query and the fragment, up to the next space whatever
    characters the query holds. Words right after a cut query that look like more of it (a = or &) go too."""
    out, in_query = [], False
    for tok in re.split(r"(\s+)", s):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        if in_query and ("=" in tok or "&" in tok) and not URL_START_RE.search(tok):
            continue
        tok, in_query = clean_token(tok)
        out.append(tok)
    return "".join(out)


def clean_str(s, limit):
    s = SURROGATE_RE.sub("�", str(s))
    s = CTRL_RE.sub(" ", ANSI_RE.sub("", s))
    s = clean_urls(s)
    for v in known_secrets():
        if v in s:
            s = s.replace(v, "[redacted]")
    if _known["tags"]:
        s = redact_tagged(s)
    s = re.sub(r"  +", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def clean_data(obj, depth=0):
    if depth > MAX["depth"]:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:MAX["keys"]]:
            k = clean_str(k, 64)
            if not k or SECRET_KEY_RE.search(k):
                continue
            out[k] = clean_data(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [clean_data(v, depth + 1) for v in list(obj)[:MAX["list"]]]
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    return clean_str(obj, MAX["string"])


def clean_event_data(data):
    if not data:
        return {}
    if not isinstance(data, dict):
        data = {"value_of": str(type(data).__name__)}
    d = clean_data(data)
    if len(json.dumps(d, ensure_ascii=False).encode("utf-8")) > MAX["data_bytes"]:
        d = {"truncated": True, "keys": sorted(d)[:MAX["keys"]]}
    return d


# --- events ---------------------------------------------------------------------------
def now_iso(ts=None):
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_to_ns(t):
    try:
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%fZ")
        return calendar.timegm(dt.timetuple()) * 10**9 + dt.microsecond * 1000
    except (TypeError, ValueError):
        return time.time_ns()


def caller_project():
    """The project folder of the Claude Code session that ran this command, else the current folder."""
    try:
        import owners
        pid = owners.claude_of(os.getpid(), owners.ps_table())
        if pid:
            cwd = owners.proc_cwd(pid)
            if cwd:
                return cwd
    except Exception:
        pass
    try:
        return os.getcwd()
    except OSError:
        return None


def build_event(family, type_, message, severity=None, action=None, data=None, vm=None, project=None, at=None,
                origin="cli"):
    if family not in FAMILIES:
        raise EventsError(f"unknown family {family!r} ({', '.join(FAMILIES)})")
    if not TYPE_RE.match(type_ or ""):
        raise EventsError(f"event types are lowercase letters, digits and _ (got {type_!r})")
    severity = severity or DEFAULT_SEVERITY[family]
    if severity not in SEVERITIES:
        raise EventsError(f"severity must be one of {', '.join(SEVERITIES)}")
    if origin not in ORIGINS:
        raise EventsError(f"origin must be one of {', '.join(ORIGINS)}")
    return {
        "id": str(uuid.uuid4()),
        "seq": None,
        "time": now_iso(at),
        "family": family,
        "type": type_,
        "severity": severity,
        "origin": origin,
        "vm": clean_str(vm or vm_name(), 64),
        "project": clean_str(project, 300) if project else None,
        "message": clean_str(message or "", MAX["message"]) or type_.replace("_", " "),
        "action": clean_str(action, MAX["action"]) if action else None,
        "data": clean_event_data(data),
        "host": clean_str(host_name(), 64),
    }


def _last_seq_in(path):
    """The seq of the last complete line of a JSON-lines file (reads only its tail)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 65536))
            lines = f.read().split(b"\n")[:-1]
        for line in reversed(lines):
            try:
                v = json.loads(line).get("seq")
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
            except (ValueError, AttributeError):
                continue
    except OSError:
        pass
    return 0


def next_seq(cfg=None):
    """The next sequence number. Never goes back below what the sinks already have, even if the counter file
    was lost or restored from an older copy. Hold the global lock."""
    p = os.path.join(state_dir(), "seq")
    try:
        n = int(open(p).read().strip() or 0)
    except (OSError, ValueError):
        n = max([_last_seq_in(log_path())] + [_last_seq_in(f) for f in _queue_files()])
    for name in (cfg or {}).get("sinks", {}):
        n = max(n, int(load_status(name).get("sent_seq") or 0))
    n += 1
    write_private(p, str(n))
    return n


def _queue_files():
    d = os.path.join(state_dir(), "sinks")
    try:
        return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".queue")]
    except OSError:
        return []


def current_seq():
    try:
        return int(open(os.path.join(state_dir(), "seq")).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def append_log(ev):
    p = log_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        if os.path.getsize(p) > LOG_MAX:
            os.replace(p, p + ".1")
    except OSError:
        pass
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def appended(name):
    try:
        return int(open(sink_file(name, "appended")).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def rate_exempt(ev, only_sinks):
    return (only_sinks is not None or ev["family"] == "actions" or ev["type"] == "suppressed"
            or (ev["family"], ev["type"]) in RATE_EXEMPT)


def take_token(family, now=None):
    """The per-family token bucket (RATE_CAP per RATE_WINDOW). Returns (allowed, suppressed count to report
    now). Hold the global lock."""
    now = now or time.time()
    p = os.path.join(state_dir(), "ratelimit.json")
    rl = read_json(p, {})
    rl = rl if isinstance(rl, dict) else {}
    b = rl.get(family) if isinstance(rl.get(family), dict) else {}
    tokens = min(RATE_CAP, float(b.get("tokens", RATE_CAP)) + (now - float(b.get("at", now))) * RATE_CAP / RATE_WINDOW)
    report = 0
    if tokens >= 1:
        tokens -= 1
        allowed = True
        report, b["suppressed"] = int(b.get("suppressed", 0)), 0
    else:
        allowed = False
        b["suppressed"] = int(b.get("suppressed", 0)) + 1
        b.setdefault("since", now)
    if report:
        b.pop("since", None)
    b["tokens"], b["at"] = tokens, now
    rl[family] = b
    write_private(p, json.dumps(rl))
    return allowed, report


def suppressed_event(family, count, origin="cli"):
    label = FAMILY_LABEL.get(family, family)
    return build_event(family, "suppressed",
                       f"{count} more '{label}' event(s) weren't sent: at most {RATE_CAP} per "
                       f"{RATE_WINDOW // 60} minutes reach the sinks (all are in the local log)",
                       "info", data={"suppressed": count, "limit": f"{RATE_CAP} per {RATE_WINDOW // 60} min"},
                       origin=origin)


def _queue(ev, cfg, only_sinks):
    line = (json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    for name, sink in cfg.get("sinks", {}).items():
        if (only_sinks is not None and name not in only_sinks) or (only_sinks is None and not matches(sink, ev)):
            continue
        append_private(sink_file(name, "queue"), line)
        write_private(sink_file(name, "appended"), str(appended(name) + 1))


def record(ev, cfg, only_sinks=None):
    """Number the event, log it and queue it for each sink it matches (or exactly `only_sinks`), within the
    family's rate limit. Hold the global lock. Raises before using a number if the event can't be written."""
    dumps(ev).encode("utf-8")                   # fails here, not after a number is taken
    allowed, report = (True, 0) if rate_exempt(ev, only_sinks) else take_token(ev["family"])
    if report:                                  # first say how many were held back since the last one sent
        s_ev = suppressed_event(ev["family"], report, ev.get("origin", "cli"))
        s_ev["seq"] = next_seq(cfg)
        append_log(s_ev)
        _queue(s_ev, cfg, None)
    ev["seq"] = next_seq(cfg)
    if not allowed:
        ev["rate_limited"] = True
    if ev["family"] != "actions":               # the signed action log already has those
        append_log(ev)
    if allowed:
        _queue(ev, cfg, only_sinks)
    return ev


def flush_suppressed(cfg):
    """A summary for each family whose bucket held events back and has a token again. Hold the global lock."""
    p = os.path.join(state_dir(), "ratelimit.json")
    rl = read_json(p, {})
    for family, b in (rl.items() if isinstance(rl, dict) else []):
        if family in FAMILIES and isinstance(b, dict) and int(b.get("suppressed", 0)) > 0:
            tokens = min(RATE_CAP, float(b.get("tokens", 0)) + (time.time() - float(b.get("at", 0))) * RATE_CAP / RATE_WINDOW)
            if tokens >= 1:
                n = int(b["suppressed"])
                b["suppressed"], b["tokens"], b["at"] = 0, tokens - 1, time.time()
                rl[family] = b
                write_private(p, json.dumps(rl))
                s_ev = suppressed_event(family, n, "watcher")
                s_ev["seq"] = next_seq(cfg)
                append_log(s_ev)
                _queue(s_ev, cfg, None)


def debounced(key, seconds):
    """True when the same key was marked within `seconds`. Hold the global lock."""
    marks = read_json(os.path.join(state_dir(), "debounce.json"), {})
    v = marks.get(key) if isinstance(marks, dict) else None
    return isinstance(v, (int, float)) and time.time() - v < seconds


def mark_debounce(key):
    """Remember that `key` was just emitted. Hold the global lock."""
    p = os.path.join(state_dir(), "debounce.json")
    marks = read_json(p, {})
    now = time.time()
    marks = {k: v for k, v in (marks.items() if isinstance(marks, dict) else [])
             if isinstance(v, (int, float)) and now - v < 86400}
    marks[key] = now
    write_private(p, json.dumps(marks))


# --- the spool (other producers, e.g. the secrets vault) ------------------------------
def spool_event(obj, mtime):
    """A spool file's event, validated and cleaned, or None."""
    if not isinstance(obj, dict):
        return None
    fam, typ = obj.get("family"), obj.get("type")
    if fam not in FAMILIES or not isinstance(typ, str) or not TYPE_RE.match(typ):
        return None
    msg = obj.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return None
    sev = obj.get("severity") if obj.get("severity") in SEVERITIES else None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else None
    vm = obj.get("vm") if isinstance(obj.get("vm"), str) and obj.get("vm") else None
    action = obj.get("action") if isinstance(obj.get("action"), str) else None
    project = obj.get("project") if isinstance(obj.get("project"), str) else None
    return build_event(fam, typ, msg, sev, action, data, vm, project, at=mtime, origin="spool")


def drain_spool(cfg):
    """Turn every spool file into an event (oldest name first) and delete it. Hold the global lock.
    A file that can't be read or recorded is deleted and counted; it never stops the others."""
    d = spool_dir()
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return 0, 0
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        note_error(f"spool {d} is a link or not yours; not reading it")
        return 0, 0
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)
    done = rejected = 0
    for f in sorted(x for x in os.listdir(d) if x.endswith(".json") and not x.startswith("."))[:500]:
        p = os.path.join(d, f)
        try:
            fst = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.getuid():
            continue
        ev = None
        if fst.st_size <= SPOOL_MAX_BYTES:
            try:
                with open(p, encoding="utf-8") as fh:
                    ev = spool_event(json.load(fh), fst.st_mtime)
            except (OSError, ValueError, EventsError):
                ev = None
        try:
            os.remove(p)
        except OSError:
            continue
        if ev:
            try:
                record(ev, cfg)
                done += 1
                continue
            except Exception as e:
                note_error(f"spool {f}: {e!r}")
        rejected += 1
    if rejected:
        s = os.path.join(state_dir(), "spool.json")
        info = read_json(s, {})
        info["rejected"] = int(info.get("rejected", 0)) + rejected
        info["last_rejected_at"] = now_iso()
        write_private(s, json.dumps(info))
        note_error(f"spool: {rejected} file(s) were not valid events and were deleted")
    return done, rejected


# --- formats ---------------------------------------------------------------------------------
def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def cloudevent(ev):
    return {"specversion": "1.0", "id": ev["id"], "source": f"metacomputer/{ev['host']}",
            "type": f"{CE_PREFIX}.{ev['family']}.{ev['type']}", "time": ev["time"], "subject": ev["vm"],
            "datacontenttype": "application/json", "data": ev}


def signature(key_b64, body):
    return "sha256=" + hmac.new(base64.b64decode(key_b64), body, hashlib.sha256).hexdigest()


_version = {}


def service_version():
    if "v" not in _version:
        v = "unknown"
        try:
            r = subprocess.run(["git", "-C", LAB, "describe", "--tags", "--match", "v*", "--always", "--dirty"],
                               capture_output=True, text=True, timeout=2)
            v = r.stdout.strip() or v
        except (OSError, subprocess.TimeoutExpired):
            pass
        _version["v"] = v
    return _version["v"]


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}      # int64 as a string (protobuf JSON)
    return {"key": key, "value": {"stringValue": str(value)}}


def otlp_body(events):
    host = events[0]["host"] if events else host_name()
    observed = str(time.time_ns())
    recs = []
    for ev in events:
        num, text = OTLP_SEV[ev["severity"]]
        attrs = [_attr("metacomputer.family", ev["family"]), _attr("metacomputer.type", ev["type"]),
                 _attr("metacomputer.vm", ev["vm"]), _attr("metacomputer.seq", int(ev["seq"])),
                 _attr("metacomputer.origin", ev.get("origin") or "cli")]
        if ev.get("project"):
            attrs.append(_attr("metacomputer.project", ev["project"]))
        recs.append({"timeUnixNano": str(iso_to_ns(ev["time"])), "observedTimeUnixNano": observed,
                     "severityNumber": num, "severityText": text, "body": {"stringValue": dumps(ev)},
                     "attributes": attrs})
    return {"resourceLogs": [{
        "resource": {"attributes": [_attr("service.name", "metacomputer"), _attr("service.version", service_version()),
                                    _attr("host.name", host)]},
        "scopeLogs": [{"scope": {"name": "metacomputer.events", "version": "1"}, "logRecords": recs}]}]}


def _sd_value(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _ascii(s, limit):
    s = "".join(c for c in str(s) if 33 <= ord(c) <= 126)
    return s[:limit] or "-"


def syslog_frame(ev):
    pri = SYSLOG_FACILITY * 8 + SYSLOG_SEV[ev["severity"]]
    sd = (f'[{SD_ID} seq="{ev["seq"]}" family="{ev["family"]}" origin="{_sd_value(ev.get("origin") or "cli")}" '
          f'vm="{_sd_value(ev["vm"])}" id="{ev["id"]}"]')
    msg = (f"<{pri}>1 {ev['time']} {_ascii(ev['host'], 255)} metacomputer - {_ascii(ev['type'], 32)} {sd} "
           + dumps(ev)).encode("utf-8")
    return str(len(msg)).encode() + b" " + msg


FAMILY_LABEL = {"needs_you": "needs you", "task": "task", "security": "security", "lifecycle": "lifecycle",
                "actions": "action"}
NTFY_TAGS = {"needs_you": "raising_hand", "task": "white_check_mark", "security": "shield", "lifecycle": "computer",
             "actions": "memo"}


def ntfy_title(ev):
    return f"{ev['vm']} - {FAMILY_LABEL[ev['family']]}: {ev['type'].replace('_', ' ')}"


def ntfy_payload(url, ev):
    u = urlsplit(url)
    base, _, topic = u.path.rstrip("/").rpartition("/")
    prio = 5 if ev["family"] == "needs_you" else {"info": 3, "warn": 4, "error": 4}[ev["severity"]]
    tags = [NTFY_TAGS[ev["family"]]] + (["warning"] if ev["severity"] == "warn" else
                                        ["rotating_light"] if ev["severity"] == "error" else [])
    msg = ev["message"] + (f" -> {ev['action']}" if ev.get("action") else "")
    endpoint = f"{u.scheme}://{u.netloc}{base}/" + (f"?{u.query}" if u.query else "")   # keeps ?auth=
    return endpoint, {"topic": topic, "title": ntfy_title(ev), "message": msg, "priority": prio, "tags": tags}


# --- delivery ---------------------------------------------------------------------------------
def tls_context(ca_pem):
    if ca_pem:
        ctx = ssl.create_default_context(cadata=ca_pem)            # trust only this CA
        # Private CAs often lack the extensions X.509 "strict" mode wants; the chain and the
        # host name are still checked.
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    else:
        ctx = ssl.create_default_context()
        if not ctx.cert_store_stats().get("x509_ca") and os.path.exists("/etc/ssl/cert.pem"):
            ctx.load_verify_locations("/etc/ssl/cert.pem")         # macOS's own bundle
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def short_error(e):
    if isinstance(e, socket.timeout) or "timed out" in str(e):
        return "timed out"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, ssl.SSLCertVerificationError):
        return "TLS: " + (e.verify_message or "certificate not trusted")
    return (type(e).__name__ + ": " + str(e))[:200]


def http_post(url, body, headers, ca_pem, timeout):
    """POST without redirects or proxies. Returns (status, error)."""
    u = urlsplit(url)
    conn = None
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=timeout, context=tls_context(ca_pem))
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        conn.request("POST", path, body=body, headers=headers)
        r = conn.getresponse()
        r.read(65536)
        return r.status, None
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as e:
        return None, short_error(e)
    finally:
        if conn is not None:
            conn.close()


def otlp_url(url):
    """URL + /v1/logs, before any query string, unless it already ends with /v1/logs."""
    u = urlsplit(url)
    path = u.path.rstrip("/")
    path = path if path.endswith("/v1/logs") else path + "/v1/logs"
    return f"{u.scheme}://{u.netloc}{path}" + (f"?{u.query}" if u.query else "")


def deliver(sink, batch, timeout):
    """Send events to one sink. Returns (ok, error, permanent): permanent = this batch will never be accepted."""
    kind, url = sink["kind"], sink["url"]
    try:
        check_url(kind, url)                 # events.json may have been edited by hand
    except EventsError as e:
        return False, f"the URL isn't allowed: {e}", False
    extra = {k: v for k, v in sink.get("headers") or []}
    if kind == "syslog":
        # Syslog over TCP has no acknowledgement: success means TLS took every byte and the connection
        # closed without an error. A server that drops messages after that isn't noticed (best effort).
        u = urlsplit(url)
        try:
            raw = socket.create_connection((u.hostname, u.port or 6514), timeout=timeout)
            try:
                tls = tls_context(sink.get("ca_pem")).wrap_socket(raw, server_hostname=u.hostname)
                tls.sendall(b"".join(syslog_frame(ev) for ev in batch))
                try:
                    tls.settimeout(0.5)
                    tls.unwrap()                 # close_notify, so the server sees a clean end
                except (OSError, ssl.SSLError, ValueError):
                    pass
                tls.close()
            finally:
                raw.close()
            return True, None, False
        except (OSError, ssl.SSLError, ValueError) as e:
            return False, short_error(e), False
    if kind == "webhook":
        ev = batch[0]
        body = dumps(cloudevent(ev)).encode()
        headers = {**extra, "Content-Type": "application/cloudevents+json; charset=utf-8", "User-Agent": USER_AGENT,
                   SIGNATURE_HEADER: signature(sink["hmac_key"], body)}
        target = url
    elif kind == "otlp":
        body = dumps(otlp_body(batch)).encode()
        headers = {**extra, "Content-Type": "application/json", "User-Agent": USER_AGENT}
        target = otlp_url(url)
    elif kind == "ntfy":
        target, payload = ntfy_payload(url, batch[0])
        body = dumps(payload).encode()
        headers = {**extra, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    else:
        return False, f"unknown kind {kind}", False
    status, err = http_post(target, body, headers, sink.get("ca_pem"), timeout)
    if err:
        return False, err, False
    if 200 <= status < 300:
        return True, None, False
    return False, f"HTTP {status}", status in (400, 413, 415, 422)


def backoff(failures):
    return min(15 * 2 ** max(failures - 1, 0), 600)


def read_queue(name, after):
    try:
        with open(sink_file(name, "queue"), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    out = []
    for line in data.split(b"\n")[:-1]:        # complete lines only
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("seq"), int) and ev["seq"] > after:
            out.append(ev)
    return out


STATUS0 = {"sent_seq": 0, "delivered": 0, "failures": 0, "last_error": None, "last_error_at": None,
           "last_ok_at": None, "next_retry": 0, "dropped": 0, "head_attempts": 0, "single_until": 0}


def load_status(name):
    st = dict(STATUS0)
    got = read_json(sink_file(name, "status.json"), {})
    st.update(got if isinstance(got, dict) else {})
    return st


def save_status(name, st):
    write_private(sink_file(name, "status.json"), json.dumps(st))


def backlog(name, st=None):
    """Events waiting for this sink, from counters (no parsing): queued - delivered - dropped."""
    st = st or load_status(name)
    return max(0, appended(name) - int(st.get("delivered") or 0) - int(st.get("dropped") or 0))


def compact(name, sent_seq):
    """Drop the lines already sent. Hold the sink's lock; takes the global lock (appends happen under it)."""
    p = sink_file(name, "queue")
    try:
        size = os.path.getsize(p)
    except OSError:
        return
    with global_lock():
        rest = read_queue(name, sent_seq)
        if not rest:
            os.truncate(p, 0)
        elif size > 2**20:
            write_private(p, "".join(dumps(ev) + "\n" for ev in rest))


def flush_sink(name, sink, budget=EMIT_BUDGET, force=False, wait_lock=1.0):
    """Send this sink's backlog in order, within `budget` seconds. Returns its status (None if busy)."""
    q = sink_file(name, "queue")
    try:
        if os.path.getsize(q) == 0:
            return None
    except OSError:
        return None
    try:
        lock = Lock(sink_file(name, "lock"), wait_lock)
        lock.__enter__()
    except Busy:
        return None         # another process is sending this sink's backlog; it picks up new events too
    try:
        st = load_status(name)
        now = time.time()
        if not force and st["next_retry"] > now:
            return st
        deadline = now + budget
        pending, i, emptied = read_queue(name, st["sent_seq"]), 0, False
        if len(pending) > MAX_BACKLOG:
            drop = len(pending) - MAX_BACKLOG
            st["sent_seq"], st["dropped"] = pending[drop - 1]["seq"], st["dropped"] + drop
            pending = pending[drop:]
            save_status(name, st)
        while True:
            if i >= len(pending):
                pending, i = read_queue(name, st["sent_seq"]), 0
                if not pending:
                    emptied = True
                    break
            left = deadline - time.time()
            if left < 0.3:
                break
            size = BATCH.get(sink["kind"], 1)
            if pending[i]["seq"] <= st.get("single_until", 0):
                size = 1                              # after a refused batch: one at a time past it
            batch = pending[i:i + size]
            ok, err, permanent = deliver(sink, batch, min(TIMEOUT, left))
            if ok:
                st.update(sent_seq=batch[-1]["seq"], delivered=st["delivered"] + len(batch), failures=0,
                          next_retry=0, last_ok_at=time.time(), head_attempts=0)
                i += len(batch)
            else:
                st.update(failures=st["failures"] + 1, last_error=err, last_error_at=time.time(),
                          next_retry=time.time() + backoff(st["failures"] + 1))
                if permanent and len(batch) > 1:
                    # One bad event shouldn't cost the batch: retry these one by one, right away.
                    st.update(single_until=batch[-1]["seq"], next_retry=0, failures=st["failures"] - 1)
                elif permanent:
                    st["head_attempts"] += 1
                    if st["head_attempts"] >= 3:      # this one will never be accepted: skip it, keep the rest moving
                        st.update(sent_seq=batch[-1]["seq"], dropped=st["dropped"] + len(batch), head_attempts=0,
                                  next_retry=0, last_error=f"{err}; dropped {len(batch)} event(s) it refused 3 times")
                save_status(name, st)
                if permanent and len(batch) > 1:
                    continue
                break
            save_status(name, st)
        if emptied or os.path.getsize(q) > 2**20:
            compact(name, st["sent_seq"])
        return st
    finally:
        lock.__exit__()


def flush_all(cfg, budget=EMIT_BUDGET, force=False, names=None):
    """Every sink with a backlog, in parallel; returns within about budget + 1 s."""
    threads = []
    for name, sink in cfg.get("sinks", {}).items():
        if names is not None and name not in names:
            continue
        try:
            if os.path.getsize(sink_file(name, "queue")) == 0:
                continue
        except OSError:
            continue

        def run(n=name, s=sink):
            try:
                flush_sink(n, s, budget, force)
            except Exception as e:  # never let one sink stop the others
                note_error(f"sink {n}: {e!r}")
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    end = time.monotonic() + budget + 1.0
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))


# --- emitting -------------------------------------------------------------------------------
def emit(family, type_, message, severity=None, action=None, data=None, vm=None, project=None,
         debounce=None, debounce_s=DEBOUNCE_S, deliver_now=True, only_sinks=None, budget=EMIT_BUDGET,
         raise_errors=False, origin="cli"):
    """Record one event and try to deliver it. Best effort: returns the event, or None (debounced, not
    wanted, or a problem, which is noted for `events status`). Never raises unless raise_errors.
    only_sinks: exactly these sinks, whatever their filters (and outside the rate limit)."""
    try:
        cfg = load_config()
        if family == "actions" and only_sinks is None and not wants(cfg, "actions"):
            return None
        ev = build_event(family, type_, message, severity, action, data, vm,
                         project if project is not None else _default_project(), origin=origin)
        key = f"{family}.{type_}:{debounce}" if debounce else None
        with global_lock():
            if key and debounced(key, debounce_s):
                return None
            drain_spool(cfg)
            record(ev, cfg, only_sinks)
            if key:
                mark_debounce(key)             # only once the event is safely recorded
        if deliver_now:
            flush_all(cfg, budget)
        return ev
    except Exception as e:
        if raise_errors:
            raise
        note_error(f"emit {family}.{type_}: {e!r}")
        return None


def _default_project():
    try:
        return os.getcwd()
    except OSError:
        return None


_async = {"q": None, "thread": None}


def _worker():
    q = _async["q"]
    while True:
        items = [q.get()]
        while len(items) < 200:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        try:
            for a, kw in items:
                emit(*a, **dict(kw, deliver_now=False))
            flush_all(load_config())
        except Exception as e:
            note_error(f"async emit: {e!r}")
        finally:
            for _ in items:
                q.task_done()


def emit_async(*a, **kw):
    """emit() on a background thread, for long-lived processes (the MCP bridges) that must never wait."""
    if _async["q"] is None:
        _async["q"] = queue.Queue()
        _async["thread"] = threading.Thread(target=_worker, daemon=True)
        _async["thread"].start()
    _async["q"].put((a, kw))


def flush_async(timeout=2.0):
    """Before a bridge exits: wait for queued events; any the worker hasn't taken by then are recorded here
    (into the backlogs, without sending), so the watcher or the next emission delivers them."""
    q = _async["q"]
    end = time.monotonic() + timeout
    while q is not None and q.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.05)
    while q is not None:
        try:
            a, kw = q.get_nowait()
        except queue.Empty:
            break
        try:
            emit(*a, **dict(kw, deliver_now=False))
        finally:
            q.task_done()


def drain(force=False, budget=WATCH_BUDGET):
    cfg = load_config()
    with global_lock():
        n, bad = drain_spool(cfg)
        flush_suppressed(cfg)
    flush_all(cfg, budget, force)
    return n, bad


# --- the watcher ----------------------------------------------------------------------------
def watch_state_path(vm):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", vm)[:64] or "vm"
    return os.path.join(state_dir(), f"watch-{safe}.json")


class NetgwTail:
    """New lines of the gateway's blocked-connections log (logs/netgw-blocked.jsonl)."""

    def __init__(self, path, st):
        self.path, self.st = path, st
        if "netgw_offset" not in st:            # first run: start at the end, don't report history
            try:
                s = os.stat(path)
                st["netgw_offset"], st["netgw_inode"] = s.st_size, s.st_ino
            except OSError:
                st["netgw_offset"], st["netgw_inode"] = 0, None

    def poll(self):
        try:
            s = os.stat(self.path)
        except OSError:
            return []
        off = self.st.get("netgw_offset", 0)
        if not isinstance(off, int) or s.st_ino != self.st.get("netgw_inode") or s.st_size < off:
            off = 0                              # a new or truncated file
        with open(self.path, "rb") as f:
            f.seek(off)
            data = f.read(4 * 2**20)
        cut = data.rfind(b"\n") + 1
        self.st["netgw_offset"], self.st["netgw_inode"] = off + cut, s.st_ino
        out = []
        for line in data[:cut].split(b"\n"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("event", "blocked") == "blocked":
                out.append(r)
        return out


def split_dst(dst):
    dst = str(dst or "")
    if dst.startswith("["):
        host, _, rest = dst[1:].partition("]")
        return host, rest.lstrip(":") or None
    if dst.count(":") == 1:
        h, p = dst.split(":")
        return h, p
    return dst, None


def _port(p):
    try:
        p = str(p or "")
        return int(p) if p.isdecimal() and p.isascii() and 0 < int(p) < 65536 else None
    except ValueError:
        return None


def add_blocked(st, recs):
    """Count blocked connections by destination, port, protocol and reason (never payloads)."""
    pend = st.get("netgw_pending")
    pend = pend if isinstance(pend, list) else []
    index = {(e.get("host"), e.get("port"), e.get("proto"), e.get("reason")): e for e in pend if isinstance(e, dict)}
    for r in recs:
        if not isinstance(r, dict):
            continue
        host, port = split_dst(r.get("dst"))
        k = (str(host)[:64], _port(port), str(r.get("proto") or "")[:8] or None, str(r.get("reason") or "")[:64] or None)
        e = index.get(k)
        if e is None:
            if len(index) >= 200:
                k = ("other", None, None, None)
                e = index.get(k)
            if e is None:
                e = {"host": k[0], "port": k[1], "proto": k[2], "reason": k[3], "count": 0,
                     "first": r.get("t") if isinstance(r.get("t"), str) else None}
                index[k] = e
        e["count"] = int(e.get("count") or 0) + 1
        e["last"] = r.get("t") if isinstance(r.get("t"), str) else e.get("last")
    st["netgw_pending"] = list(index.values())


def blocked_event(st, vm=None, now=None):
    """The batched security event for the pending blocked connections, or None (then they wait).
    At most one a minute; when the same destinations come back each time, the wait doubles, up to an hour."""
    pend = [e for e in (st.get("netgw_pending") or []) if isinstance(e, dict)]
    now = now or time.time()
    if not pend:
        return None
    sig = hashlib.sha256(dumps(sorted([str(e.get("host")), str(e.get("port")), str(e.get("proto")),
                                       str(e.get("reason"))] for e in pend)).encode()).hexdigest()[:16]
    same = sig == st.get("netgw_sig")
    repeat = int(st.get("netgw_repeat") or 0) if same else 0
    wait = min(NETGW_BATCH_S * 2 ** min(repeat + 1, 10), NETGW_MAX_S) if same else NETGW_BATCH_S
    if now - float(st.get("netgw_last") or 0) < wait:
        return None
    dests = sorted(pend, key=lambda e: -int(e.get("count") or 0))
    total = sum(int(d.get("count") or 0) for d in dests)
    top = ", ".join(f"{d['host']}{':' + str(d['port']) if d.get('port') else ''} ({d.get('reason') or 'blocked'})"
                    + (f" x{d['count']}" if d["count"] > 1 else "") for d in dests[:3])
    st["netgw_pending"], st["netgw_last"], st["netgw_sig"] = [], now, sig
    st["netgw_repeat"] = repeat + 1 if same else 0
    return {"family": "security", "type": "blocked_connections", "severity": "warn", "vm": vm,
            "message": f"The VM tried {total} connection(s) its gateway blocks: {top}"
                       + (f" and {len(dests) - 3} more destination(s)" if len(dests) > 3 else ""),
            "data": {"count": total, "destinations": dests[:20], "log": "logs/netgw-blocked.jsonl",
                     "same_as_before": same}}


HEALTH_TEXT = {
    "vm_off": ("error", "The virtual computer is not running (state: {vm}).", "metacomputer start, or metacomputer status"),
    "gateway": ("error", "The VM's network gateway (cua-netgw) is down.", "metacomputer restart"),
    "ssh": ("error", "The VM is running but doesn't answer SSH.", "metacomputer status; metacomputer restart if it stays down"),
    "not_ready": ("warn", "The VM's desktop session isn't ready (Xorg, XFCE or accessibility).", "metacomputer status"),
    "doctor": ("warn", "Cua Driver's doctor check is failing (rc={rc}).", "metacomputer status"),
    "disk_low": ("warn", "Only {disk} GB free on the VM's disk.", "free space in the VM (metacomputer inspect files), or reset it if you agree"),
    "recording_failed": ("error", "The desktop recording '{rec}' stopped on its own.", "metacomputer record stop, to save what it captured"),
}


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def health_conditions(h):
    """Problems in a `metacomputer _health` answer: {condition: (severity, message, action)}.
    Values that come from inside the VM are checked: only numbers, never its text."""
    out = {}
    fmt = {"vm": str(h.get("vm"))[:32], "rc": None, "disk": None, "rec": str((h.get("recording") or {}).get("name"))[:64]}

    def put(k, **kv):
        sev, msg, act = HEALTH_TEXT[k]
        out[k] = (sev, msg.format(**dict(fmt, **kv)), act)
    if h.get("vm") != "running":
        put("vm_off")
    else:
        if not h.get("gateway"):
            put("gateway")
        if not h.get("ssh"):
            put("ssh")
        else:
            gs = h.get("guest") if isinstance(h.get("guest"), dict) else {}
            if not gs.get("ready"):
                put("not_ready")
            rc = gs.get("driver_doctor_rc")
            if rc is not None:
                valid = isinstance(rc, int) and not isinstance(rc, bool)
                if not valid or rc != 0:
                    put("doctor", rc=rc if valid else "unknown")
            disk = gs.get("disk_free_gb")
            if _number(disk) and disk < 3:
                put("disk_low", disk=round(float(disk), 1))
    rec = h.get("recording") if isinstance(h.get("recording"), dict) else {}
    if rec.get("expected") and rec.get("running") is False:
        put("recording_failed")
    return out


def health_events(st, h, vm=None, now=None):
    """One event per condition that started or cleared since the last probe. A condition that comes back
    within HEALTH_RENOTIFY_S of its last announcement isn't announced again (nor its recovery)."""
    now = now or time.time()
    cur = health_conditions(h)
    before = st.get("health") if isinstance(st.get("health"), dict) else {}
    told = st.get("health_told") if isinstance(st.get("health_told"), dict) else {}
    quiet = set(st.get("health_quiet") or [])
    evs = []
    for k, (sev, msg, act) in cur.items():
        if k in before:
            continue
        if now - float(told.get(k) or 0) < HEALTH_RENOTIFY_S:
            quiet.add(k)                       # flapping: stay quiet about this episode
            continue
        quiet.discard(k)
        told[k] = now
        evs.append({"family": "lifecycle", "type": "health", "severity": sev, "vm": vm, "message": msg,
                    "action": act, "data": {"condition": k, "state": "problem"}})
    for k in before:
        if k not in cur:
            if k in quiet:
                quiet.discard(k)
                continue
            evs.append({"family": "lifecycle", "type": "health", "severity": "info", "vm": vm,
                        "message": f"Recovered: {k.replace('_', ' ')}.", "data": {"condition": k, "state": "ok"}})
    st["health"] = {k: v[0] for k, v in cur.items()}
    st["health_told"], st["health_quiet"] = told, sorted(quiet)
    return evs


def probe_health():
    r = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "_health"], capture_output=True, text=True,
                       timeout=240)
    h = json.loads(r.stdout or "{}")
    return h if isinstance(h, dict) else {}


def emit_dict(e, project=None, origin="cli"):
    return emit(e["family"], e["type"], e["message"], e.get("severity"), e.get("action"), e.get("data"),
                e.get("vm"), project, deliver_now=False, origin=origin)


def watch_cycle(st, tail, vm, health=False):
    """One cycle. Each step is on its own, so a problem in one (an odd gateway record, a failed probe)
    doesn't stop the others."""
    def step(name, fn):
        try:
            fn()
        except Exception as e:
            note_error(f"watcher {name}: {e!r}")

    def spool():
        cfg = load_config()
        with global_lock():
            drain_spool(cfg)
            flush_suppressed(cfg)

    def blocked():
        add_blocked(st, tail.poll())
        ev = blocked_event(st, vm)
        if ev:
            emit_dict(ev, origin="watcher")

    def probe():
        st["health_last"] = time.time()
        for e in health_events(st, probe_health(), vm):
            emit_dict(e, origin="watcher")
    step("spool", spool)
    step("blocked connections", blocked)
    if health:
        step("health", probe)
    step("delivery", lambda: flush_all(load_config(), WATCH_BUDGET))


def watcher_pid():
    """The running watcher's pid for this VM, or None."""
    try:
        pid = int(open(watcher_pidfile()).read().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return pid if "events.py" in cmd and "watch" in cmd else None


def watch(once=False, health=False):
    vm = vm_name()
    stop = {"now": False}
    pf = watcher_pidfile()
    pid_fd = None
    if not once:
        # The pidfile stays locked while this watcher lives, so a second one can't start.
        os.makedirs(os.path.dirname(pf), exist_ok=True)
        pid_fd = os.open(pf, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(pid_fd)
            print(f"a watcher is already running for {vm}", file=sys.stderr)
            return 0
        os.fchmod(pid_fd, 0o600)
        os.ftruncate(pid_fd, 0)
        os.write(pid_fd, str(os.getpid()).encode())
        import signal

        def on_signal(*_):
            stop["now"] = True
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} watcher started for {vm} (pid {os.getpid()})", flush=True)
    sp = watch_state_path(vm)
    st = read_json(sp, {})
    st = st if isinstance(st, dict) else {}
    if not once or "health_last" not in st:
        st["health_last"] = time.time()           # the first probe 5 minutes after start
    tail = NetgwTail(os.path.join(log_dir(), "netgw-blocked.jsonl"), st)
    try:
        while not stop["now"]:
            due = health or (not once and time.time() - float(st.get("health_last") or 0) >= HEALTH_INTERVAL)
            watch_cycle(st, tail, vm, health=due)
            try:
                write_private(sp, json.dumps(st))
            except Exception as e:
                note_error(f"watcher state: {e!r}")
            if once:
                break
            try:
                if not load_config().get("sinks"):
                    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} no sinks configured; watcher exits", flush=True)
                    break
            except Exception as e:
                note_error(f"watcher config: {e!r}")
            for _ in range(WATCH_INTERVAL):
                if stop["now"]:
                    break
                time.sleep(1)
    finally:
        if pid_fd is not None:
            try:
                if open(pf).read().strip() == str(os.getpid()):
                    os.remove(pf)
            except OSError:
                pass
            os.close(pid_fd)
    return 0


# --- helpers for other parts of metacomputer ----------------------------------------------
def action_record_event(rec, path=None):
    """The `actions` event for one signed action-log record: tool, refused or not, hashes. Never arguments."""
    t = rec.get("type") or "record"
    tool = rec.get("tool")
    data = {"record_type": t, "record_seq": rec.get("seq"), "hash": rec.get("hash"), "prev": rec.get("prev")}
    if path:
        data["file"] = os.path.basename(path)
    if tool:
        data["tool"] = tool
    if t == "result":
        res = rec.get("result") or {}
        data["error"] = bool(res.get("error") or rec.get("rpc_error"))
        data["ms"] = rec.get("ms")
        msg = f"{tool} {'failed' if data['error'] else 'returned'}"
    elif t == "refused":
        data["refused"] = True
        msg = f"{tool} refused by the bridge"
    elif t == "call":
        msg = f"{tool} called"
    elif t == "checkpoint":
        data["covers_seq"], data["final"] = rec.get("covers_seq"), rec.get("final")
        msg = f"action log signed through record {rec.get('covers_seq')}"
    else:
        msg = f"action log: {t}"
    typ = t if TYPE_RE.match(str(t)) else "record"
    return {"family": "actions", "type": typ, "severity": "warn" if t in ("refused", "sign_failed") else "info",
            "message": msg, "data": data}


def recording_event(mp4):
    """task.recording_stopped from a desktop recording and its description file."""
    side = read_json(mp4[:-4] + ".json" if mp4.endswith(".mp4") else mp4 + ".json", {})
    dur = side.get("duration_s")
    name = side.get("name") or os.path.basename(mp4)[:-4]
    return {"family": "task", "type": "recording_stopped", "severity": "info",
            "message": f"Recording '{name}' saved" + (f" ({dur:.0f} s)" if isinstance(dur, (int, float)) else ""),
            "data": {"name": name, "duration_s": dur, "path": mp4,
                     "description_file": (mp4[:-4] + ".json") if side else None,
                     "key_moments": len(side.get("moments") or []), "audio": bool(side.get("audio")),
                     "mode": side.get("mode"), "bytes": side.get("bytes")}}


# --- CLI ---------------------------------------------------------------------------------------
KEEP_SEGMENTS = {"v1", "v2", "logs", "api", "hooks", "hook", "webhook", "webhooks", "services", "ingest", "events",
                 "otlp", "collector", "notify"}
NOTIFY_DUP_S = 300


def mask_url(url):
    """A URL to show: scheme, host and port, and only path parts that can't be secrets (webhook URLs and
    ntfy topics often are)."""
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port
    except ValueError:
        return "(unreadable URL)"
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    parts = [p if p.lower() in KEEP_SEGMENTS else "***" for p in u.path.split("/") if p]
    return f"{u.scheme}://{netloc}" + ("/" + "/".join(parts) if parts else "") + ("?***" if u.query else "")


def check_url(kind, url):
    try:
        u = urlsplit(url)
        u.port                                  # raises for a port outside 0-65535
    except ValueError as e:
        raise EventsError(f"not a usable URL ({e})")
    if u.username or u.password:
        raise EventsError("put credentials in --header, not in the URL")
    if u.fragment:
        raise EventsError("the URL can't have a #fragment")
    if u.port == 0:
        raise EventsError("port 0 isn't a port")
    if kind == "syslog":
        if u.scheme != "syslog+tls" or not u.hostname:
            raise EventsError("syslog sinks are TLS only: syslog+tls://HOST:PORT (RFC 5425; port 6514 by default)")
        if u.path not in ("", "/") or u.query:
            raise EventsError("a syslog URL is just syslog+tls://HOST:PORT")
        return
    if u.scheme == "https" and u.hostname:
        pass
    elif u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        pass
    else:
        raise EventsError("use https:// (http:// only to this Mac's loopback, e.g. a local Collector or test receiver)")
    if kind == "ntfy" and not u.path.strip("/"):
        raise EventsError("an ntfy URL names the topic: https://ntfy.sh/YOUR-TOPIC")


def parse_header(h):
    name, sep, value = h.partition(":")
    name, value = name.strip(), value.strip()
    if not sep or not HEADER_NAME_RE.match(name) or not value or any(c in value for c in "\r\n\0"):
        raise EventsError("--header takes 'Name: value'")
    return [name, value]


def parse_header_env(h):
    name, sep, tmpl = h.partition("=")
    name = name.strip()
    if not sep or not HEADER_NAME_RE.match(name) or not tmpl.strip():
        raise EventsError("--header-env takes 'Name=VAR' or 'Name=Prefix $VAR'")
    if "$" not in tmpl:
        tmpl = "$" + tmpl.strip()
    missing = []

    def sub(m):
        var = m.group(1) or m.group(2)
        if var not in os.environ:
            missing.append(var)
            return ""
        return os.environ[var]
    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, tmpl).strip()
    if missing:
        raise EventsError(f"--header-env: {', '.join(missing)} not set in the environment")
    if not value or any(c in value for c in "\r\n\0"):
        raise EventsError("--header-env: empty or multi-line value")
    return [name, value]


def parse_families(s):
    fams = [f.strip() for f in (s or "all").split(",") if f.strip()]
    bad = [f for f in fams if f != "all" and f not in FAMILIES]
    if bad or not fams:
        raise EventsError(f"families: {', '.join(FAMILIES)} or all (all = every family except actions)")
    return fams


def ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def sink_summary(name, sink):
    st = load_status(name)
    waiting = backlog(name, st)
    return {"name": name, "kind": sink["kind"], "url": mask_url(sink["url"]),
            "families": sink.get("families") or ["all"], "min_severity": sink.get("min_severity") or "info",
            "headers": [h[0] for h in sink.get("headers") or []], "ca": bool(sink.get("ca_pem")),
            "signed": sink["kind"] == "webhook", "created": sink.get("created"), "backlog": waiting,
            "delivered": st["delivered"], "dropped": st["dropped"], "last_ok_at": st["last_ok_at"],
            "last_error": st["last_error"] if st["failures"] or waiting else None,
            "last_error_at": st["last_error_at"], "next_retry_in": max(0, int(st["next_retry"] - time.time()))}


def confirm_change(a, question):
    """Adding, replacing or removing a sink decides what leaves this Mac: only from a terminal, so a program
    without one (an agent's shell, a script) can't do it quietly."""
    if not sys.stdin.isatty():
        raise EventsError(f"changing event sinks decides what leaves this Mac, so it needs your own terminal: run "
                          f"`metacomputer events sink {a.sub} ...` there (add --yes to skip the question)")
    if a.yes:
        return
    sys.stderr.write(question + " [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        raise EventsError("cancelled")


def announce_sink_change(type_, name, sink, prior, extra=None):
    """security.sink_added / sink_removed: to the local log and to every sink that existed before the
    change (whatever their filters), sent now."""
    verb = {"sink_added": "added", "sink_removed": "removed"}[type_]
    data = {"sink": name, "kind": sink.get("kind"), "url": mask_url(sink.get("url") or ""),
            "families": sink.get("families") or ["all"], "min_severity": sink.get("min_severity") or "info",
            "from_terminal": True, **(extra or {})}
    ev = emit("security", type_, f"Event sink '{name}' {verb}: {data['kind']} to {data['url']}, "
                                 f"families {','.join(data['families'])}", "warn",
              "If you didn't do this, check metacomputer events sink list", data, project=caller_project(),
              only_sinks=set(prior), deliver_now=False, raise_errors=True)
    flush_all(load_config(), EMIT_BUDGET, names=set(prior))
    return ev


def remove_sink_files(name):
    for ext in ("queue", "status.json", "lock", "appended"):
        try:
            os.remove(sink_file(name, ext))
        except OSError:
            pass


def cmd_sink(a):
    cfg = load_config()
    sinks = cfg["sinks"]
    if a.sub == "add":
        if not NAME_RE.match(a.name):
            raise EventsError("sink names: lowercase letters, digits, - and _ (max 32)")
        if a.name in sinks and not a.replace:
            raise EventsError(f"a sink named {a.name} exists (--replace to overwrite it)")
        check_url(a.kind, a.url)
        headers = [parse_header(h) for h in a.header or []] + [parse_header_env(h) for h in a.header_env or []]
        if a.kind == "syslog" and headers:
            raise EventsError("syslog has no headers")
        ca_pem = None
        if a.ca:
            ca_pem = open(a.ca, encoding="ascii").read()
            if "BEGIN CERTIFICATE" not in ca_pem:
                raise EventsError(f"{a.ca}: not a PEM certificate")
            ssl.create_default_context(cadata=ca_pem)
        sink = {"kind": a.kind, "url": a.url, "headers": headers, "ca_pem": ca_pem,
                "families": parse_families(a.families), "min_severity": a.min_severity, "created": now_iso()}
        if a.kind == "webhook":
            sink["hmac_key"] = base64.b64encode(secrets.token_bytes(32)).decode()
        replacing = a.name in sinks
        dropped = backlog(a.name) if replacing else 0
        confirm_change(a, f"{'Replace' if replacing else 'Add'} event sink '{a.name}': {a.kind} to {mask_url(a.url)}, "
                          f"families {','.join(sink['families'])}, from {a.min_severity}?")
        announce_sink_change("sink_added", a.name, sink, sinks, {"replaced": replacing})
        with global_lock():
            cfg = load_config()
            cfg["sinks"][a.name] = sink
            save_config(cfg)
            remove_sink_files(a.name)
            save_status(a.name, dict(STATUS0, sent_seq=current_seq()))
        _known["at"] = 0
        fams = ",".join(sink["families"])
        print(f"{'replaced' if replacing else 'added'} sink {a.name}: {a.kind} to {mask_url(a.url)}; families {fams}, "
              f"from {a.min_severity}" + (f"; headers {', '.join(h[0] for h in headers)} (values hidden)" if headers else ""))
        if replacing:
            print(f"  the old {a.name}'s signing key no longer works, and {dropped} event(s) still waiting for it "
                  "were dropped")
        if a.kind == "webhook":
            print(f"signing key for {SIGNATURE_HEADER} (HMAC-SHA256 over the body, key = these 32 bytes, base64):")
            print(f"  {sink['hmac_key']}")
            print(f"  shown again with: metacomputer events sink show {a.name} --key")
        print(f"The configuration is {config_path()} (0600). Anything running as you can read it, so use "
              "ingest-only tokens. Try it: metacomputer events sink test " + a.name)
        return 0
    if a.sub == "list":
        rows = [sink_summary(n, s) for n, s in sorted(sinks.items())]
        if a.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("no event sinks (nothing is sent anywhere). Add one: metacomputer events sink add NAME --kind "
                  "webhook|otlp|syslog|ntfy --url URL  (docs/events.md)")
            return 0
        for r in rows:
            state = (f"error {ago(r['last_error_at'])}: {r['last_error']}"
                     + (f" (retry in {r['next_retry_in']} s)" if r["next_retry_in"] else "")) if r["last_error"] else (
                f"ok {ago(r['last_ok_at'])}" if r["last_ok_at"] else "nothing sent yet")
            print(f"{r['name']:<14} {r['kind']:<8} {r['url']:<40} {','.join(r['families']):<24} "
                  f"from {r['min_severity']:<5} backlog {r['backlog']:<5} {state}")
            if r["headers"]:
                print(f"{'':14} headers: " + ", ".join(f"{h}: (hidden)" for h in r["headers"]))
        return 0
    if a.name not in sinks:
        raise EventsError(f"no sink named {a.name} (metacomputer events sink list)")
    sink = sinks[a.name]
    if a.sub == "show":
        r = sink_summary(a.name, sink)
        if a.key:
            if sink["kind"] != "webhook":
                raise EventsError("only webhook sinks sign their requests")
            r["hmac_key"] = sink["hmac_key"]
        if a.json:
            print(json.dumps(r, indent=2))
            return 0
        for k, v in r.items():
            if k == "headers":
                v = ", ".join(f"{h}: (hidden)" for h in v) or "-"
            print(f"{k:<14} {v}")
        return 0
    if a.sub == "remove":
        confirm_change(a, f"Remove event sink '{a.name}' ({sink['kind']} to {mask_url(sink['url'])})? "
                          f"{backlog(a.name)} event(s) waiting for it are dropped.")
        announce_sink_change("sink_removed", a.name, sink, sinks)      # the removed sink is told too
        with global_lock():
            cfg = load_config()
            cfg["sinks"].pop(a.name, None)
            save_config(cfg)
            remove_sink_files(a.name)
        print(f"removed sink {a.name}")
        return 0
    if a.sub == "test":
        ev = emit("lifecycle", "test", f"Test event for sink '{a.name}' from metacomputer on {host_name()}",
                  only_sinks={a.name}, deliver_now=False, project=caller_project(), raise_errors=True)
        end, st = time.time() + 60, load_status(a.name)
        while time.time() < end and st["sent_seq"] < ev["seq"]:
            before = st["sent_seq"]
            st = flush_sink(a.name, sink, budget=10, force=True, wait_lock=5) or load_status(a.name)
            if st["sent_seq"] == before:
                break                          # no progress: the sink refused or is unreachable
        if st["sent_seq"] >= ev["seq"]:
            print(f"OK: {a.name} accepted test event {ev['seq']}")
            return 0
        if st["sent_seq"] > 0 and not st["failures"]:
            print(f"NOT YET: {a.name} is still taking {backlog(a.name, st)} earlier event(s); the test event "
                  "follows them (metacomputer events sink list)")
            return 1
        print(f"FAILED: {a.name}: {st['last_error'] or 'not delivered'}; it stays in the backlog and is retried")
        return 1
    return 2


def fmt_line(ev):
    try:
        t = datetime.strptime(ev["time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).astimezone()
        ts = t.strftime("%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError, TypeError):
        ts = str(ev.get("time"))
    return (f"{ev.get('seq', ''):>6} {ts} {str(ev.get('severity', '')).upper():<5} "
            f"{str(ev.get('family')) + '.' + str(ev.get('type')):<32} {str(ev.get('origin') or '-'):<7} "
            f"{ev.get('vm')}: {ev.get('message')}" + (f"  -> {ev['action']}" if ev.get("action") else "")
            + ("  (rate-limited: not sent)" if ev.get("rate_limited") else ""))


def cmd_tail(a):
    p = log_path()
    try:
        # split on "\n" only: a stray line separator inside a value must not hide an event
        lines = [x for x in open(p, encoding="utf-8").read().split("\n") if x][-a.n:]
    except OSError:
        print(f"no events yet ({p})")
        return 0
    for line in lines:
        if a.json:
            print(line)
            continue
        try:
            print(fmt_line(json.loads(line)))
        except ValueError:
            continue
    return 0


def delivery_report(cfg, ev):
    out = []
    for name, sink in sorted(cfg.get("sinks", {}).items()):
        if not matches(sink, ev):
            continue
        st = load_status(name)
        if ev.get("rate_limited"):
            out.append((name, "held back (rate limit)"))
            continue
        out.append((name, "delivered" if st["sent_seq"] >= ev["seq"] else
                    f"queued ({st['last_error'] or 'retried later'})"))
    return out


def content_key(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:16]


def cmd_emit(a, family, type_, message, severity, action, data):
    if family == "actions" and not wants(load_config(), "actions"):
        if not a.quiet:
            print("not recorded: no sink takes actions events")
        return 0
    key = content_key(family, type_, message, action, json.dumps(data, sort_keys=True, default=str))
    ev = emit(family, type_, message, severity, action, data, project=caller_project(), raise_errors=True,
              debounce=key if a.debounce > 0 else None, debounce_s=a.debounce)
    if a.quiet:
        return 0
    if ev is None:
        print(f"not sent again: the same event was emitted within {a.debounce} s")
        return 0
    cfg = load_config()
    rep = delivery_report(cfg, ev)
    if rep:
        print(f"event {ev['seq']} ({family}.{type_}): " + "; ".join(f"{n} {s}" for n, s in rep))
    else:
        print(f"event {ev['seq']} ({family}.{type_}) recorded in {log_path()}; no sink takes it, so nothing was sent")
    return 0


def cmd_notify(a):
    msg = " ".join(a.text).strip()
    if not msg:
        raise EventsError('usage: metacomputer notify "what you need" [--why WHY] [--action "what to do"]')
    ev = emit("needs_you", a.why, msg, "warn", a.action, {"why": a.why, "declared_by": "claude"},
              project=caller_project(), raise_errors=True, debounce=content_key(a.why, msg, a.action),
              debounce_s=NOTIFY_DUP_S)
    if ev is None:
        print(f"Not sent again: the same message went out within {NOTIFY_DUP_S // 60} minutes. Say it in your reply, "
              "and wait for the user.")
        return 0
    cfg = load_config()
    rep = delivery_report(cfg, ev)
    if rep:
        print("notified: " + "; ".join(f"{n} {s}" for n, s in rep))
    else:
        print("No event sink takes needs_you events, so nobody was pinged (it's in the local log: metacomputer "
              "events tail). Say it in your reply too, and wait for the user.")
    return 0


def status_info():
    out = {"config": config_path(), "log": log_path(), "spool": spool_dir(), "sinks": 0, "names": [],
           "watcher": {"running": False, "pid": None}, "backlog": 0, "failing": [], "spool_waiting": 0, "errors": []}
    try:
        cfg = load_config()
    except Exception as e:
        out["errors"].append(str(e))
        return out
    out["sinks"] = len(cfg["sinks"])
    out["names"] = sorted(cfg["sinks"])
    for n in out["names"]:
        st = load_status(n)
        b = backlog(n, st)
        out["backlog"] += b
        if b and st["last_error"]:
            out["failing"].append({"sink": n, "error": st["last_error"], "backlog": b})
    pid = watcher_pid()
    out["watcher"] = {"running": bool(pid), "pid": pid}
    try:
        out["spool_waiting"] = sum(1 for f in os.listdir(spool_dir()) if f.endswith(".json") and not f.startswith("."))
    except OSError:
        pass
    out["spool_rejected"] = int(read_json(os.path.join(state_dir(), "spool.json"), {}).get("rejected", 0))
    rl = read_json(os.path.join(state_dir(), "ratelimit.json"), {})
    out["rate_limited"] = {f: int(b.get("suppressed", 0)) for f, b in (rl.items() if isinstance(rl, dict) else [])
                           if isinstance(b, dict) and int(b.get("suppressed", 0))}
    try:
        out["errors"] = open(os.path.join(state_dir(), "errors.log")).read().splitlines()[-3:]
    except OSError:
        pass
    return out


def status_line(s):
    if not s["sinks"]:
        return "off (no sinks; metacomputer events sink add)"
    line = (f"{s['sinks']} sink{'s' if s['sinks'] != 1 else ''}, watcher "
            f"{'running' if s['watcher']['running'] else 'not running'}, backlog {s['backlog']}")
    if s["failing"]:
        line += "; failing: " + ", ".join(f"{f['sink']} ({f['error']})" for f in s["failing"])
    if s.get("rate_limited"):
        line += "; held back by the rate limit: " + ", ".join(f"{f} {n}" for f, n in s["rate_limited"].items())
    return line


def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer events", description="Optional event stream (docs/events.md).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sk = sub.add_parser("sink").add_subparsers(dest="sub", required=True)
    p = sk.add_parser("add", help="needs your own terminal")
    p.add_argument("name")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--url", required=True)
    p.add_argument("--header", action="append", help="'Name: value' (lands in shell history; see --header-env)")
    p.add_argument("--header-env", action="append", help="'Name=VAR' or 'Name=Bearer $VAR': the value from the environment")
    p.add_argument("--ca", help="PEM file: trust only this CA for the sink's TLS")
    p.add_argument("--families", default="all")
    p.add_argument("--min-severity", default="info", choices=SEVERITIES)
    p.add_argument("--replace", action="store_true", help="overwrite a sink of that name (new key, its backlog dropped)")
    p.add_argument("--yes", action="store_true", help="don't ask (still needs a terminal)")
    p = sk.add_parser("list")
    p.add_argument("--json", action="store_true")
    p = sk.add_parser("show")
    p.add_argument("name")
    p.add_argument("--key", action="store_true", help="also print a webhook sink's signing key")
    p.add_argument("--json", action="store_true")
    sk.add_parser("test").add_argument("name")
    p = sk.add_parser("remove", help="needs your own terminal")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="don't ask (still needs a terminal)")
    p = sub.add_parser("tail")
    p.add_argument("n", nargs="?", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("emit")
    p.add_argument("family", choices=FAMILIES)
    p.add_argument("type")
    p.add_argument("--message", required=True)
    p.add_argument("--severity", choices=SEVERITIES)
    p.add_argument("--action")
    p.add_argument("--data", help="a small JSON object")
    p.add_argument("--field", action="append", help="KEY=VALUE added to data (repeatable; digits become numbers)")
    p.add_argument("--debounce", type=int, default=EMIT_DUP_S,
                   help=f"drop the same event again within this many seconds (default {EMIT_DUP_S}; 0: never)")
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("notify")
    p.add_argument("text", nargs="+")
    p.add_argument("--why", default="other", choices=WHY)
    p.add_argument("--action")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--line", action="store_true")
    p = sub.add_parser("drain")
    p.add_argument("--now", action="store_true", help="ignore the backoff and retry every backlog now")
    p = sub.add_parser("watch")
    p.add_argument("--once", action="store_true")
    p.add_argument("--health", action="store_true")
    sub.add_parser("has-sinks")
    p = sub.add_parser("_recording")
    p.add_argument("mp4")
    p = sub.add_parser("_remember-redaction")       # a value on stdin to redact (kept only as a short keyed tag)
    p.add_argument("label")
    a = ap.parse_args(argv or ["status"])
    try:
        if a.cmd == "sink":
            return cmd_sink(a)
        if a.cmd == "tail":
            return cmd_tail(a)
        if a.cmd == "emit":
            data = None
            if a.data:
                data = json.loads(a.data)
                if not isinstance(data, dict):
                    raise EventsError("--data takes a JSON object")
            for kv in a.field or []:
                k, sep, v = kv.partition("=")
                if not sep or not k:
                    raise EventsError("--field takes KEY=VALUE")
                data = data or {}
                data[k] = int(v) if v.isdecimal() and v.isascii() and len(v) < 16 else v
            return cmd_emit(a, a.family, a.type, a.message, a.severity, a.action, data)
        if a.cmd == "notify":
            return cmd_notify(a)
        if a.cmd == "status":
            s = status_info()
            print(json.dumps(s, indent=2) if a.json else status_line(s))
            if not a.json and not a.line and s["errors"]:
                print("recent problems in the event code (events-state/errors.log):")
                for line in s["errors"]:
                    print("  " + line)
            return 0
        if a.cmd == "drain":
            n, bad = drain(force=a.now)
            print(f"spool: {n} event(s) taken" + (f", {bad} invalid file(s) deleted" if bad else "")
                  + "; " + status_line(status_info()))
            return 0
        if a.cmd == "watch":
            return watch(once=a.once, health=a.health)
        if a.cmd == "has-sinks":
            return 0 if load_config().get("sinks") else 1
        if a.cmd == "_recording":
            emit_dict(recording_event(a.mp4), caller_project())
            flush_all(load_config())
            return 0
        if a.cmd == "_remember-redaction":
            if not re.match(r"^[a-z0-9_-]{1,32}$", a.label):
                raise EventsError("label: lowercase letters, digits, - and _")
            value = sys.stdin.readline().rstrip("\n")
            if len(value) >= 6:
                remember_redaction(a.label, value)
            return 0
    except (EventsError, ValueError, OSError, Busy) as e:
        print(f"metacomputer events: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
