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
                                                     (origin script, also for metacomputer's shell hooks;
                                                      metacomputer's own types are refused)
  events.py notify TEXT [--why login|captcha|2fa|dialog|approval|payment|other] [--action TEXT]
  events.py status [--json|--line]
  events.py drain [--now]                            the spool, then every sink's backlog
  events.py watch [--once [--health]]                the watcher (`metacomputer start` runs it)
  events.py has-sinks                                exit 0 when a sink is configured
  events.py needs-watcher                            exit 0 when a watcher is wanted (sinks, or announcements to deliver)

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
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flockwait  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAMILIES = ("needs_you", "task", "security", "lifecycle", "actions")
ALL = ("needs_you", "task", "security", "lifecycle")          # "all"; actions is opt-in only
DEFAULT_SEVERITY = {"needs_you": "warn", "task": "info", "security": "warn", "lifecycle": "info", "actions": "info"}
SEVERITIES = ("info", "warn", "error")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
KINDS = ("webhook", "otlp", "syslog", "ntfy")
WHY = ("login", "captcha", "2fa", "dialog", "approval", "payment", "other")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")

# Which code path made an event, set by that code, never by the caller: a metacomputer command (cli), Claude
# Code's bridge or the action-log relay (bridge), the watcher, another program through the spool, or
# `metacomputer events emit` (script: anything that can run a command can call it).
ORIGINS = ("cli", "bridge", "watcher", "spool", "script", "claude")
# Types only their own code path may produce: `events emit` and spool files can't use them (nor the
# `actions` family, nor `suppressed` in any family), so a made-up event can't look like a real one or claim
# a real one's place outside the limits.
RESERVED = {("needs_you", "in_use"), ("needs_you", "refused"), ("security", "chrome_input_refused"),
            ("lifecycle", "controller_connected"), ("lifecycle", "controller_released"),
            ("security", "blocked_connections"), ("lifecycle", "health"),
            ("security", "sink_added"), ("security", "sink_removed"), ("security", "sink_config_changed"),
            ("lifecycle", "test"),
            ("security", "lesson_approved"), ("security", "lesson_removed"), ("security", "lesson_edited"),
            ("security", "lessons_resealed"),
            ("lifecycle", "storage_attached"), ("security", "symlink_neutralized"),
            ("security", "special_file_removed"), ("security", "quarantine_restored"),
            ("security", "storage_protection_failed"), ("task", "s3_conflict"), ("task", "s3_sync_failed"),
            ("lifecycle", "handoff_opened"), ("lifecycle", "handoff_done"), ("lifecycle", "handoff_cancelled"),
            ("lifecycle", "handoff_timed_out"), ("security", "hidden_text"), ("security", "net_allowlist_started"),
            ("security", "net_allowlist_changed"), ("security", "net_allowlist_lifted")}
RESERVED_TYPES = {"suppressed"}
ADMIN_TYPES = {"sink_added", "sink_removed", "sink_config_changed"}

TIMEOUT = 3.0           # per request
EMIT_BUDGET = 3.0       # per sink, at the point of emission
WATCH_BUDGET = 20.0     # per sink, per watcher cycle
WATCH_INTERVAL = 30
HEALTH_INTERVAL = 300
HEALTH_RENOTIFY_S = 1800  # a health condition that comes back within 30 min isn't announced again
NETGW_BATCH_S = 60        # follow-ups: at most one blocked-connections event a minute ...
NETGW_MAX_S = 3600        # ... and, for the same destinations again and again, doubling up to once an hour
NETGW_QUIET_S = 300       # the first blocked connection after 5 minutes with none goes out at once ...
NETGW_FIRST_CAP = (6, 3600)   # ... at most 6 times in any hour; past that it waits like a follow-up
NETGW_POLL_S = 2          # between its cycles, the watcher looks at the gateway's log this often
DEBOUNCE_S = 300
EMIT_DUP_S = 60           # `events emit`: the same event again within a minute is dropped
RATE_CAP, RATE_WINDOW = 10, 600   # per family: at most 10 events in any 10 minutes reach the sinks
# Per phone (ntfy) sink, whatever the family, tests and summaries included: at most 10 in any 10 minutes
# and 30 in any hour.
PHONE_LIMITS = ((10, 600), (30, 3600))
ADMIN_MAX_ATTEMPTS = 20           # a sink-change announcement is retried this often (with backoff), then given up
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


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


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
    # MCOMP_EVENTS_SPOOL (yours), else this VM's own (EVENTS_SPOOL, resolved by bin/metacomputer), else the default.
    return os.environ.get("MCOMP_EVENTS_SPOOL") or os.environ.get("MCOMP_VM_EVENTS_SPOOL") or os.path.expanduser(
        "~/Library/Application Support/metacomputer/events/spool")


def log_dir():
    return os.environ.get("MCOMP_LOG_DIR") or os.path.join(LAB, "logs")


def log_path():
    return os.path.join(log_dir(), "events.jsonl")


def run_dir():
    return os.environ.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run")


def watcher_pidfile():
    return os.path.join(run_dir(), "events-watcher.pid")


def gen_of(sink):
    """A sink's generation: a new one each time it's added or replaced, so files of a retired version are
    never mixed with the current one's."""
    g = (sink or {}).get("generation")
    return g if isinstance(g, str) and re.fullmatch(r"[a-z0-9]{1,32}", g) else "g0"


def sink_file(name, gen, ext):
    """A file of one generation of a sink: its backlog (queue), queued count (appended), delivery status."""
    return os.path.join(state_dir(), "sinks", f"{name}.{gen}.{ext}")


def sink_lock(name):
    """One lock per sink name, kept across generations: whoever sends a sink's backlog, or retires it, holds it."""
    return os.path.join(state_dir(), "sinks", f"{name}.lock")


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
        try:
            flockwait.exclusive(self.fd, self.wait, 0.02)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise Busy(self.path)
        return self

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


_config_cache = {"key": None, "cfg": None}


def config_for_reading():
    """load_config() for a caller that only reads it, on a hot path (the relay's `actions` check runs once
    per action-log record): parsed again only when events.json's inode, size or mtime changes. The same
    object is returned each time: don't change it."""
    p = config_path()
    try:
        st = os.lstat(p)
        key = (p, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_uid)
    except FileNotFoundError:
        key = (p, None)
    if key != _config_cache["key"]:
        cfg = load_config()                  # its checks (link, owner, mode) run whenever the file changes
        _config_cache.update(key=key, cfg=cfg)
    return _config_cache["cfg"]


def render_config(cfg):
    return json.dumps(cfg, indent=2, sort_keys=True) + "\n"


def save_config(cfg, text=None):
    """Write the sinks (only `events sink ...` does) and remember them as the known configuration, so a
    change made any other way is noticed (config_guard). Hold the global lock."""
    private_dir(events_dir())
    text = text or render_config(cfg)
    write_private(config_path(), text)
    remember_config(text)      # after: a crash in between is recognised as our own change (its journal's hash)


def config_safe():
    """The configuration, or none (nothing is sent) if events.json can't be read: then events still reach
    the local log, `events status` says why nothing is sent, and the change was announced (config_guard)."""
    try:
        return load_config()
    except Exception as e:
        note_error(f"events.json can't be read ({e}); events are only logged locally until it's fixed")
        return {"version": 1, "sinks": {}}


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
def _cls(*ranges):
    """A regex character class from code-point ranges (kept as numbers, so the source stays plain ASCII)."""
    return "[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "") for a, b in ranges) + "]"


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x9b[0-?]*[ -/]*[@-~]")
# Control characters, soft hyphen, Arabic letter mark, zero-width and bidirectional controls, line and
# paragraph separators, invisible operators, the BOM and tag characters.
CTRL_RE = re.compile(_cls((0x00, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x61C, 0x61C), (0x200B, 0x200F),
                          (0x2028, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069), (0xFEFF, 0xFEFF), (0xE0000, 0xE007F)))
SURROGATE_RE = re.compile(_cls((0xD800, 0xDFFF)))
SCHEMES_NO_SLASH = ("mailto", "tel", "sms", "data", "javascript", "about", "blob", "file", "news", "urn", "magnet",
                    "intent")
HOST_RE = (r"(?:(?:[A-Za-z0-9-]+\.)+[A-Za-z][A-Za-z0-9-]*|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]|localhost)")
# Where a link starts inside a word: scheme://, //host, a scheme without slashes (mailto:, data:, ...), or a
# host (name.with.dots, an IPv4 address, [an:IPv6:address], localhost) followed by / ? # or ;.
URL_START_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{1,15}://"
    r"|(?<![:/\w])//(?=[\w\[])"
    rf"|(?<![A-Za-z0-9+.-])(?:{'|'.join(SCHEMES_NO_SLASH)}):"
    rf"|(?<![\w@.:\[-]){HOST_RE}(?::\d+)?(?=[/?#;])", re.I)
# user:password@host anywhere (with or without a scheme before it)
USERINFO_RE = re.compile(rf"(?<![\w.%+-])([A-Za-z0-9._~%+!$'()*,;-]+):([^\s/?#@]*)@(?={HOST_RE})", re.I)
QUERY_LIKE_RE = re.compile(r"\?[^?]*[=&]")
PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
SECRET_KEY_RE = re.compile(
    r"pass(word|wd|phrase|code)?s?$|passcode|pwd|secret|token|cookie|header|credential|api_?key|private_?key|"
    r"session|jwt|bearer|(^|_)key$|(^|_)sig(nature)?($|_)|(^|_)auth(orization)?($|_)|_code$|"
    r"^(text|value|values|script|typed|otp|totp|code|pin|vnc.*)$")
MAX = {"message": 500, "action": 300, "string": 300, "list": 50, "keys": 50, "depth": 4, "data_bytes": 4096}
_known = {"at": 0.0, "values": []}


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
    """Values that must never appear in an event: sink header values, signing keys and secret URL parts, and
    the live-view password when it's kept in a file ($SSH_DIR/vnc-pass). When it's kept in your own secret
    store it is never fetched for this (nothing in the event code handles it)."""
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
    _known["at"] = time.time()
    return _known["values"]


def _strip_userinfo(m):
    return m.group(0) if m.group(1).lower() in SCHEMES_NO_SLASH else ""


def clean_token(tok):
    """One word with every link in it cut down: no user:password@, no ;params, nothing from ? or # on.
    Returns (word, cut a query)."""
    out, i, first = [], 0, True
    while True:
        m = URL_START_RE.search(tok, i)
        if not m:
            rest = tok[i:]
            if first:
                q = QUERY_LIKE_RE.search(rest)           # "p?token=x" without a recognisable host
                if q:
                    return "".join(out) + rest[:q.start()], True
            out.append(rest)
            return "".join(out), False
        first = False
        out.append(tok[i:m.start()])
        lead, j = m.group(0), m.end()
        nxt = URL_START_RE.search(tok, j)
        end = nxt.start() if nxt else len(tok)
        part = tok[j:end]
        if lead.endswith("//"):                       # an authority: drop credentials
            a_end = min([k for k in (part.find(c) for c in "/?#;") if k >= 0], default=len(part))
            part = part[:a_end].rsplit("@", 1)[-1] + part[a_end:]
        cut = min([k for k in (part.find("?"), part.find("#")) if k >= 0], default=-1)
        if cut >= 0:                                  # the rest of the word goes, whatever it holds
            out.append(lead + re.sub(r";[^/]*", "", part[:cut]))
            return "".join(out), part[cut] == "?"
        out.append(lead + re.sub(r";[^/]*", "", part))
        i = end


def decode_words(s):
    """Every word with %XX escapes decoded once (control characters that produces become spaces)."""
    return re.sub(r"\S*%[0-9A-Fa-f]{2}\S*", lambda m: CTRL_RE.sub(" ", unquote(m.group(0))), s)


def clean_urls(s):
    """Links lose credentials, path parameters, the query and the fragment, up to the next space whatever
    characters the query holds; every link in a word, also without a scheme. Words with %XX escapes are
    decoded once first, so every decision below sees what a reader would (an encoded link, an encoded
    "token=" after a query). Words right after a cut query that look like more of it (a = or &) go as well."""
    s = USERINFO_RE.sub(_strip_userinfo, decode_words(s))
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
    s = SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", str(s))
    s = CTRL_RE.sub(" ", ANSI_RE.sub("", s))
    s = clean_urls(s)
    for v in known_secrets():
        if v in s:
            s = s.replace(v, "[redacted]")
    s = re.sub(r"  +", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def canon_key(k):
    """A key name as the secret-key rules see it: %XX escapes decoded (up to three times), invisible
    characters removed, camelCase split, - . and spaces as _, lowercase (signingKey, signing-key, pa%73sword
    and SIGNING_KEY all read as what they are)."""
    k = str(k)
    for _ in range(3):
        d = unquote(k)
        if d == k:
            break
        k = d
    k = CTRL_RE.sub("", ANSI_RE.sub("", SURROGATE_RE.sub("", k)))
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", k)
    k = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", k)
    k = re.sub(r"[-.\s]+", "_", k).lower()
    return re.sub(r"_+", "_", k).strip("_")


def clean_data(obj, depth=0):
    if depth > MAX["depth"]:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:MAX["keys"]]:
            if SECRET_KEY_RE.search(canon_key(k)):
                continue
            k = clean_str(k, 64)
            if not k:
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
    if not isinstance(type_, str) or not TYPE_RE.fullmatch(type_):
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


def reserved(family, type_):
    """True for what only metacomputer's own code paths may emit (not `events emit`, not the spool)."""
    return family == "actions" or type_ in RESERVED_TYPES or (family, type_) in RESERVED


def _last_seq_in(path):
    """The highest seq among the last lines of a JSON-lines file (reads only its tail)."""
    best = 0
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 16384))
            lines = f.read().split(b"\n")[:-1]
        for line in lines[-50:]:
            try:
                v = json.loads(line).get("seq")
            except (ValueError, AttributeError):
                continue
            if isinstance(v, int) and not isinstance(v, bool):
                best = max(best, v)
    except OSError:
        pass
    return best


def _queue_files():
    d = os.path.join(state_dir(), "sinks")
    try:
        return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".queue")]
    except OSError:
        return []


def next_seq(cfg=None):
    """The next sequence number: one more than the highest of the counter, the local log, every backlog and
    every sink's delivery status, so a lost or restored counter never goes back. Hold the global lock."""
    p = os.path.join(state_dir(), "seq")
    try:
        n = int(open(p).read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    n = max([n, _last_seq_in(log_path()), _last_seq_in(log_path() + ".1")] + [_last_seq_in(f) for f in _queue_files()])
    for name, sink in (cfg or {}).get("sinks", {}).items():
        n = max(n, int(load_status(name, gen_of(sink)).get("sent_seq") or 0))
    n += 1
    write_private(p, str(n))
    return n


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


def appended(name, gen):
    try:
        return int(open(sink_file(name, gen, "appended")).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _window(path, key, limits, now, take):
    """A strict sliding window: may another event go in `key`, under every (count, seconds) in `limits`?
    Records it when `take` and allowed. Returns (allowed, held back before this one). Hold the global lock."""
    w = read_json(path, {})
    w = w if isinstance(w, dict) else {}
    b = w.get(key) if isinstance(w.get(key), dict) else {}
    longest = max(s for _, s in limits)
    times = [t for t in b.get("times") or [] if isinstance(t, (int, float)) and now - t < longest]
    allowed = all(sum(1 for t in times if now - t < s) < n for n, s in limits)
    held = int(b.get("held") or 0)
    if take:
        if allowed:
            times.append(now)
        else:
            b["held"] = held + 1
    b["times"] = times
    w[key] = b
    write_private(path, json.dumps(w))
    return allowed, held


def _clear_held(path, key):
    w = read_json(path, {})
    if isinstance(w, dict) and isinstance(w.get(key), dict):
        w[key]["held"] = 0
        write_private(path, json.dumps(w))


def family_window():
    return os.path.join(state_dir(), "ratelimit.json")


def phone_window():
    return os.path.join(state_dir(), "phonelimit.json")


def suppressed_event(family, count, origin="cli"):
    label = FAMILY_LABEL.get(family, family)
    limit = f"{RATE_CAP} per {RATE_WINDOW // 60} min"
    return build_event(family, "suppressed", f"{count} more '{label}' event(s) weren't sent to the sinks (at most "
                       f"{limit}; all are in the local log)", "info", data={"suppressed": count, "limit": limit},
                       origin=origin)


def _put(ev, cfg, name, sink):
    """Append one event to a sink's current backlog. Hold the global lock."""
    gen = gen_of(sink)
    append_private(sink_file(name, gen, "queue"), (dumps(ev) + "\n").encode())
    write_private(sink_file(name, gen, "appended"), str(appended(name, gen) + 1))


def _queue(ev, cfg, only_sinks):
    """Queue an event for each sink it matches (or exactly `only_sinks`). Phone sinks get everything too:
    their cap applies when sending (phone_slot), so nothing queued is dropped, it waits its turn. Hold the
    global lock."""
    for name, sink in cfg.get("sinks", {}).items():
        if (only_sinks is not None and name not in only_sinks) or (only_sinks is None and not matches(sink, ev)):
            continue
        _put(ev, cfg, name, sink)


def phone_slot(name, now=None):
    """Right before a network attempt to a phone (ntfy) sink: is there room under PHONE_LIMITS? If so the
    attempt takes a slot (retries too). Returns (ok, when the next slot frees). Takes the global lock."""
    now = now or time.time()
    with global_lock():
        ok, _ = _window(phone_window(), name, PHONE_LIMITS, now, take=True)
        times = sorted((read_json(phone_window(), {}).get(name) or {}).get("times") or [])
    free_at = now
    for n, s in PHONE_LIMITS:
        recent = [t for t in times if now - t < s]
        if len(recent) >= n:
            free_at = max(free_at, recent[-n] + s)
    return ok, free_at


def record(ev, cfg, only_sinks=None, exempt=False):
    """Number the event, log it and queue it, within its family's limit unless `exempt` (only code paths set
    that: sink tests, the relay's actions events, the limit's own summaries). Hold the global lock.
    Raises before using a number if the event can't be written."""
    dumps(ev).encode("utf-8")                   # fails here, not after a number is taken
    allowed, held = True, 0
    if not exempt:
        allowed, held = _window(family_window(), ev["family"], ((RATE_CAP, RATE_WINDOW),), time.time(), take=True)
    if allowed and held:                        # first say how many were held back since the last one sent
        _clear_held(family_window(), ev["family"])
        s_ev = suppressed_event(ev["family"], held, ev.get("origin", "cli"))
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
    """A summary for each family that held events back and has room again. Hold the global lock."""
    w = read_json(family_window(), {})
    for family, b in (w.items() if isinstance(w, dict) else []):
        if family in FAMILIES and isinstance(b, dict) and int(b.get("held") or 0) > 0:
            ok, held = _window(family_window(), family, ((RATE_CAP, RATE_WINDOW),), time.time(), take=False)
            if ok and held:
                _window(family_window(), family, ((RATE_CAP, RATE_WINDOW),), time.time(), take=True)
                _clear_held(family_window(), family)
                s_ev = suppressed_event(family, held, "watcher")
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
QUARANTINE_KEEP = 50
QUARANTINED = re.compile(r"[^/.][^/]*\.json")       # what quarantine() moves aside: spool files


def spool_event(obj, mtime):
    """A spool file's event, validated and cleaned, or None. Reserved types (and `origin`) can't be claimed."""
    if not isinstance(obj, dict):
        return None
    fam, typ = obj.get("family"), obj.get("type")
    if fam not in FAMILIES or not isinstance(typ, str) or not TYPE_RE.fullmatch(typ) or reserved(fam, typ):
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


def _open_dir(name, dir_fd=None, create=False):
    """A directory as a file descriptor: never through a link, and only if it's a directory of ours
    (fixed to 0700). None if it isn't. Everything after works relative to this descriptor, so swapping the
    path for something else afterwards changes nothing."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError:
            return None
    except OSError:
        return None                         # a link (ELOOP), not a folder, or unreadable
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        return None
    if st.st_mode & 0o077:
        os.fchmod(fd, 0o700)
    return fd


def _spool_entries(dfd):
    """Names in the spool that may be events: *.json, not starting with '.', regular files of ours (checked
    without following links). A second check on the opened file follows (_read_spool_file)."""
    names = []
    for f in os.listdir(dfd):
        if not f.endswith(".json") or f.startswith("."):
            continue
        try:
            st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            names.append(f)
    return sorted(names)


def _read_spool_file(dfd, f):
    """The file's bytes, or None. Opened without following a link and without blocking (a FIFO swapped in
    after the listing can't hang us), checked again on the descriptor, and read only up to the size limit
    plus one byte (a file that grew is refused)."""
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > SPOOL_MAX_BYTES:
            return None
        chunks, size = [], 0
        while size <= SPOOL_MAX_BYTES:
            b = os.read(fd, SPOOL_MAX_BYTES + 1 - size)
            if not b:
                break
            chunks.append(b)
            size += len(b)
        data = b"".join(chunks)
        return data if len(data) <= SPOOL_MAX_BYTES else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _prune_quarantine(qfd):
    """The oldest quarantined files beyond QUARANTINE_KEEP go, through lib/safedelete.py (remove_records: the
    .quarantine folder checked first, only spool files' names)."""
    old = sorted(os.listdir(qfd))
    if len(old) > QUARANTINE_KEEP:
        remove_records(os.path.join(spool_dir(), ".quarantine"), old[:len(old) - QUARANTINE_KEEP], QUARANTINED)


def quarantine(dfd, qfd, f):
    """Move a spool file that isn't a valid event aside, into spool/.quarantine/ (qfd: opened without following a
    link, a folder of ours; the newest QUARANTINE_KEEP are kept). True when it was moved; False when it couldn't be,
    and then it stays where it is: a file that can't be quarantined is never deleted."""
    if qfd is None:
        return False
    try:
        os.rename(f, f, src_dir_fd=dfd, dst_dir_fd=qfd)
    except OSError:
        return False
    _prune_quarantine(qfd)
    return True


_SPOOL_LEFT = {}            # name -> (device, inode): files this process left in the spool (said once, then skipped)
SPOOL_REFUSED_S = 60        # a spool refused by the guard isn't checked again for this long


def _spool_refused_path():
    return os.path.join(state_dir(), "spool-refused.json")


def spool_guard(dfd):
    """None when the spool may be consumed, else why not: it's checked with lib/safedelete.py (check_spool) before a
    sweep that would delete or move anything (it may not be, hold or reach a folder some VM uses, an event spool there
    aside). A refusal is remembered for SPOOL_REFUSED_S, so emitters don't pay for the check again and again."""
    try:
        st = os.fstat(dfd)
        ident = [st.st_dev, st.st_ino]
        r = read_json(_spool_refused_path(), {})
        if isinstance(r, dict) and r.get("ident") == ident and float(r.get("until", 0)) > time.time():
            return str(r.get("why") or "refused")
    except (OSError, TypeError, ValueError):
        ident = None
    try:
        import safedelete
        safedelete.check_spool(spool_dir())
        return None
    except Exception as e:                      # Refused, or no helper: not consumed (never fails its caller)
        why = str(e) or type(e).__name__
        try:
            write_private(_spool_refused_path(), json.dumps({"ident": ident, "why": why[:300],
                                                             "until": time.time() + SPOOL_REFUSED_S}))
        except Exception:
            pass
        return why


def drain_spool(cfg):
    """Turn spool files into events (oldest name first, at most 500 a pass) and delete them. Hold the global
    lock. Everything happens relative to a pinned descriptor of the spool folder. Before anything is deleted or moved,
    the spool is checked (spool_guard): one that is, holds or reaches a folder some VM uses isn't consumed at all. A
    file that isn't a valid event, or can't be recorded, is moved to .quarantine and counted; one that can't be moved
    there (no .quarantine folder of ours) is left where it is, counted and said once, never deleted. It never stops
    (or blocks) the others."""
    dfd = _open_dir(spool_dir())
    if dfd is None:
        if os.path.lexists(spool_dir()):
            note_error(f"spool {spool_dir()} is a link, not a folder, or not yours; not reading it")
        return 0, 0
    done = rejected = 0
    left = []
    qfd = None
    try:
        names = []
        for f in _spool_entries(dfd):
            try:
                st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
                key = (st.st_dev, st.st_ino)
            except OSError:
                continue
            if _SPOOL_LEFT.get(f) == key:
                continue                        # left here before (said once): not read again
            names.append(f)
        names = names[:500]
        if not names:
            return 0, 0
        why = spool_guard(dfd)
        if why:
            note_once(f"spool-guard:{why}", f"spool {spool_dir()} isn't consumed: {why}")
            return 0, 0
        qfd = _open_dir(".quarantine", dir_fd=dfd, create=True)
        for f in names:
            raw = _read_spool_file(dfd, f)
            ev = None
            if raw is not None:
                try:
                    st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
                    ev = spool_event(json.loads(raw.decode("utf-8")), st.st_mtime)
                except Exception:               # any parse failure, RecursionError and MemoryError included
                    ev = None
            if ev:
                try:
                    record(ev, cfg)
                    os.unlink(f, dir_fd=dfd)
                    done += 1
                    continue
                except Exception as e:
                    note_error(f"spool {f}: {e!r}")
            if quarantine(dfd, qfd, f):
                rejected += 1
            else:
                left.append(f)
                try:
                    st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
                    _SPOOL_LEFT[f] = (st.st_dev, st.st_ino)
                except OSError:
                    pass
    finally:
        if qfd is not None:
            os.close(qfd)
        os.close(dfd)
    if rejected or left:
        s = os.path.join(state_dir(), "spool.json")
        info = read_json(s, {})
        info = info if isinstance(info, dict) else {}
        if rejected:
            info["rejected"] = int(info.get("rejected", 0)) + rejected
            info["last_rejected_at"] = now_iso()
        if left:
            info["left"] = int(info.get("left", 0)) + len(left)
            info["last_left_at"] = now_iso()
        write_private(s, json.dumps(info))
    if rejected:
        note_error(f"spool: {rejected} file(s) were not valid events; moved to {os.path.join(spool_dir(), '.quarantine')}")
    if left:
        note_error(f"spool: {len(left)} file(s) that aren't valid events were left in {spool_dir()} (its .quarantine "
                   "isn't a folder of yours, so they couldn't be moved aside; nothing was deleted)")
    return done, rejected


_NOTED = set()


def note_once(key, msg):
    """note_error, once per process for the same key."""
    if key not in _NOTED:
        _NOTED.add(key)
        note_error(msg)


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


NTFY_PROVENANCE = {"script": "[sent from the command line] ", "spool": "[from another program] ",
                   "claude": "[from Claude] "}


def ntfy_title(ev):
    return f"{ev['vm']} - {FAMILY_LABEL[ev['family']]}: {ev['type'].replace('_', ' ')}"


def ntfy_payload(url, ev):
    u = urlsplit(url)
    base, _, topic = u.path.rstrip("/").rpartition("/")
    prio = 5 if ev["family"] == "needs_you" else {"info": 3, "warn": 4, "error": 4}[ev["severity"]]
    tags = [NTFY_TAGS[ev["family"]]] + (["warning"] if ev["severity"] == "warn" else
                                        ["rotating_light"] if ev["severity"] == "error" else [])
    msg = ev["message"] + (f" -> {ev['action']}" if ev.get("action") else "")
    msg = NTFY_PROVENANCE.get(ev.get("origin"), "") + msg     # say where it came from when anything could have sent it
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


def http_post(url, body, headers, ca_pem, timeout, track=None):
    """POST without redirects or proxies. Returns (status, error, the first 64 KB of the answer's body).
    `track` (a list) gets a second descriptor of the socket, so deliver() can cut it off at its deadline."""
    u = urlsplit(url)
    conn = None
    try:
        # The socket is made here, so `track` can hold a second descriptor for it from the start:
        # shutting that down cuts the connection even in the middle of the TLS handshake.
        raw = socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=timeout)
        if track is not None:
            track.append(raw.dup())
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=timeout, context=tls_context(ca_pem))
            try:
                conn.sock = tls_context(ca_pem).wrap_socket(raw, server_hostname=u.hostname)
            except BaseException:
                raw.close()
                raise
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
            conn.sock = raw
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        conn.request("POST", path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read(65536)
        return r.status, None, data
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as e:
        return None, short_error(e), b""
    finally:
        if conn is not None:
            conn.close()


def otlp_rejected(body, sent):
    """(records the collector rejected, its message) from an OTLP/HTTP JSON answer: a 2xx with
    partialSuccess.rejectedLogRecords > 0 stored only part of the batch. OTLP says not to retry those."""
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return 0, ""
    ps = (d.get("partialSuccess") or d.get("partial_success")) if isinstance(d, dict) else None
    if not isinstance(ps, dict):
        return 0, ""
    raw = ps.get("rejectedLogRecords", ps.get("rejected_log_records", 0))   # an int64: a number or a string in JSON
    try:
        n = int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) else 0
    except ValueError:
        n = 0
    msg = ps.get("errorMessage") or ps.get("error_message") or ""
    return max(0, min(n, sent)), clean_str(msg, 160) if isinstance(msg, str) else ""


def otlp_url(url):
    """URL + /v1/logs, before any query string, unless it already ends with /v1/logs."""
    u = urlsplit(url)
    path = u.path.rstrip("/")
    path = path if path.endswith("/v1/logs") else path + "/v1/logs"
    return f"{u.scheme}://{u.netloc}{path}" + (f"?{u.query}" if u.query else "")


def deliver(sink, batch, timeout):
    """Send events to one sink within `timeout` seconds of wall-clock time. Returns (ok, error, permanent,
    rejected): permanent = this batch will never be accepted; rejected = how many of its events an OTLP
    collector refused in a 2xx answer (partial success: delivered, but those are dropped, not retried).

    Socket timeouts apply to each read, so a server that sends a byte every second could hold a
    connection for ever. The exchange runs in a daemon thread: past the deadline its sockets are shut
    down and this returns "timed out", so the command waiting on it (start, stop, sink add) ends on time."""
    track, box = [], {}

    def run():
        try:
            box["r"] = _deliver(sink, batch, timeout, track)
        except Exception as e:                  # never let a sender's thread die silently
            box["r"] = (False, short_error(e), False, 0)
        finally:
            for s in list(track):               # a late finish still closes what it made
                s.close()
    t = threading.Thread(target=run, daemon=True, name="events-deliver")
    t.start()
    t.join(max(0.1, timeout) + 0.5)
    done = "r" in box
    for s in list(track):                       # second descriptors of the connection (see http_post)
        if not done:
            try:
                s.shutdown(socket.SHUT_RDWR)    # cut it off: the thread then ends with an error
            except OSError:
                pass
        s.close()
    return box["r"] if done else (False, "timed out", False, 0)


def _deliver(sink, batch, timeout, track):
    kind, url = sink["kind"], sink["url"]
    try:
        check_url(kind, url)                 # events.json may have been edited by hand
    except EventsError as e:
        return False, f"the URL isn't allowed: {e}", False, 0
    extra = {k: v for k, v in sink.get("headers") or []}
    if kind == "syslog":
        # Syslog over TCP has no acknowledgement: success means TLS took every byte and the connection
        # closed without a transport or TLS error (no close_notify back, or the server just closing, is
        # fine). A server that drops messages after that isn't noticed (best effort).
        u = urlsplit(url)
        try:
            raw = socket.create_connection((u.hostname, u.port or 6514), timeout=timeout)
            track.append(raw.dup())              # a second descriptor: shutting it down cuts the connection, TLS or not
            try:
                tls = tls_context(sink.get("ca_pem")).wrap_socket(raw, server_hostname=u.hostname)
                tls.sendall(b"".join(syslog_frame(ev) for ev in batch))
                try:
                    tls.settimeout(0.5)
                    tls.unwrap()                 # close_notify, so the server sees a clean end
                except (socket.timeout, ssl.SSLZeroReturnError, ssl.SSLEOFError):
                    pass
                except (OSError, ssl.SSLError) as e:    # a reset or a TLS alert: it wasn't taken cleanly
                    return False, short_error(e), False, 0
                tls.close()
            finally:
                raw.close()
            return True, None, False, 0
        except (OSError, ssl.SSLError, ValueError) as e:
            return False, short_error(e), False, 0
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
        return False, f"unknown kind {kind}", False, 0
    status, err, answer = http_post(target, body, headers, sink.get("ca_pem"), timeout, track)
    if err:
        return False, err, False, 0
    if 200 <= status < 300:
        if kind == "otlp":
            n, msg = otlp_rejected(answer, len(batch))
            if n:
                return (True, f"the collector rejected {n} of {len(batch)} event(s) (OTLP partial success"
                        + (f": {msg}" if msg else "") + "); not retried", False, n)
        return True, None, False, 0
    return False, f"HTTP {status}", status in (400, 413, 415, 422), 0


def backoff(failures):
    return min(15 * 2 ** max(failures - 1, 0), 600)


def read_queue(name, gen, after):
    try:
        with open(sink_file(name, gen, "queue"), "rb") as f:
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
           "last_ok_at": None, "next_retry": 0, "dropped": 0, "head_attempts": 0, "single_until": 0,
           "rejected": 0, "last_rejection_at": None}


def load_status(name, gen):
    st = dict(STATUS0)
    got = read_json(sink_file(name, gen, "status.json"), {})
    st.update(got if isinstance(got, dict) else {})
    return st


def save_status(name, gen, st):
    write_private(sink_file(name, gen, "status.json"), json.dumps(st))


def backlog(name, gen, st=None):
    """Events waiting for this sink, from counters (no parsing): queued - delivered - dropped."""
    st = st or load_status(name, gen)
    return max(0, appended(name, gen) - int(st.get("delivered") or 0) - int(st.get("dropped") or 0))


def compact(name, gen, sent_seq):
    """Drop the lines already sent. Hold the sink's lock; takes the global lock (appends happen under it)."""
    p = sink_file(name, gen, "queue")
    try:
        size = os.path.getsize(p)
    except OSError:
        return
    with global_lock():
        rest = read_queue(name, gen, sent_seq)
        if not rest:
            os.truncate(p, 0)
        elif size > 2**20:
            write_private(p, "".join(dumps(ev) + "\n" for ev in rest))


def flush_sink(name, sink, budget=EMIT_BUDGET, force=False, wait_lock=1.0):
    """Send this sink's backlog in order, within `budget` seconds. Returns its status (None if busy, or if
    this generation of the sink was replaced or removed meanwhile)."""
    gen = gen_of(sink)
    q = sink_file(name, gen, "queue")
    try:
        if os.path.getsize(q) == 0:
            return None
    except OSError:
        return None
    try:
        lock = Lock(sink_lock(name), wait_lock)
        lock.__enter__()
    except Busy:
        return None         # another process is sending this sink's backlog (or retiring it)
    try:
        cur = load_config().get("sinks", {}).get(name)
        if not cur or gen_of(cur) != gen:
            return None     # replaced or removed while we waited: never send to a retired destination
        sink = cur
        st = load_status(name, gen)
        now = time.time()
        if not force and st["next_retry"] > now:
            return st
        deadline = now + budget
        pending, i, emptied = read_queue(name, gen, st["sent_seq"]), 0, False
        if len(pending) > MAX_BACKLOG:
            drop = len(pending) - MAX_BACKLOG
            st["sent_seq"], st["dropped"] = pending[drop - 1]["seq"], st["dropped"] + drop
            pending = pending[drop:]
            save_status(name, gen, st)
        while True:
            if i >= len(pending):
                pending, i = read_queue(name, gen, st["sent_seq"]), 0
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
            if sink["kind"] == "ntfy":
                room, free_at = phone_slot(name)     # every attempt takes a slot, retries included
                if not room:                         # the phone's cap: wait for a slot (not a failure)
                    st.update(next_retry=free_at, waiting="phone limit")
                    save_status(name, gen, st)
                    break
            ok, err, permanent, rejected = deliver(sink, batch, min(TIMEOUT, left))
            st.pop("waiting", None)
            if ok:
                st.update(sent_seq=batch[-1]["seq"], delivered=st["delivered"] + len(batch) - rejected, failures=0,
                          next_retry=0, last_ok_at=time.time(), head_attempts=0)
                if rejected:      # OTLP partial success: those events weren't stored, and aren't retried
                    st.update(dropped=st["dropped"] + rejected, rejected=int(st.get("rejected") or 0) + rejected,
                              last_error=err, last_error_at=time.time(), last_rejection_at=time.time())
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
                save_status(name, gen, st)
                if permanent and len(batch) > 1:
                    continue
                break
            save_status(name, gen, st)
        if emptied or os.path.getsize(q) > 2**20:
            compact(name, gen, st["sent_seq"])
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
            if os.path.getsize(sink_file(name, gen_of(sink), "queue")) == 0:
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


# --- sink changes: announcements that outlive the change; noticing hand edits -----------------
ADMIN_ACTION = "If you didn't do this, check metacomputer events sink list"


def admin_dir():
    return os.path.join(state_dir(), "admin")


def admin_log(line):
    try:
        p = os.path.join(state_dir(), "admin.log")
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            os.replace(p, p + ".1")
        append_private(p, f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n".encode())
    except Exception:
        pass


ADMIN_NAME = re.compile(r"[0-9]{15}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json")


def remove_records(folder, names, pattern):
    """Delete some of this program's own records through lib/safedelete.py: only names of pattern, in a folder that
    isn't a link and neither is, holds nor reaches a folder some VM uses. Best effort: never fails its caller."""
    try:
        import safedelete
        safedelete.remove_records(folder, names, pattern)
    except Exception:
        pass


def _admin_records():
    """(path, record) for each journaled announcement, oldest first: only journal_announcement's names (ADMIN_NAME),
    and none when the journal folder is a link or isn't a folder (it's read, rewritten and deleted from). A broken
    one is removed, through lib/safedelete.py."""
    try:
        if not stat.S_ISDIR(os.lstat(admin_dir()).st_mode):
            return []
        files = sorted(f for f in os.listdir(admin_dir()) if ADMIN_NAME.fullmatch(f))
    except OSError:
        return []
    out, broken = [], []
    for f in files:
        p = os.path.join(admin_dir(), f)
        rec = read_json(p, None)
        if not isinstance(rec, dict) or not isinstance(rec.get("targets"), dict) or not isinstance(rec.get("event"), dict):
            broken.append(f)
            continue
        out.append((p, rec))
    if broken:
        remove_records(admin_dir(), broken, ADMIN_NAME)
    return out


def journal_announcement(type_, message, data, targets, cfg_for_seq, project=None, action=ADMIN_ACTION,
                         state="committed", **extra):
    """Write a security.<type_> announcement to the local log and the journal, with a copy of each target
    sink as it is now (its old destination) and its own retry state. Hold the global lock. The configuration
    passed for numbering is the one captured by the caller, never re-read (it may be broken). Returns
    (event, journal path or None)."""
    ev = build_event("security", type_, message, "warn", action, data, project=project, origin="cli")
    ev["seq"] = next_seq(cfg_for_seq)
    append_log(ev)
    if not targets:
        admin_log(f"{type_} {ev['seq']}: no sink to tell (local log only)")
        return ev, None
    rec = {"event": ev, "created": time.time(), "state": state, **extra,
           "targets": {n: {"sink": json.loads(json.dumps(s)), "attempts": 0, "next": 0, "state": "pending",
                           "error": None} for n, s in targets.items() if isinstance(s, dict)}}
    path = os.path.join(admin_dir(), f"{int(time.time() * 1000):015d}-{ev['id']}.json")
    write_private(path, json.dumps(rec))
    return ev, path


def _config_file_hash():
    try:
        return hashlib.sha256(open(config_path(), "rb").read()).hexdigest()
    except FileNotFoundError:
        return "absent"


def recover_journal():
    """Finish what an interrupted sink change left: a 'prepared' announcement whose configuration was
    written is committed, one whose configuration never was is dropped. Only hashes the file; never loads
    it (it may be broken). Takes the global lock."""
    recs = [(p, r) for p, r in _admin_records() if r.get("state") == "prepared"]
    if not recs:
        return
    with global_lock():
        cur = _config_file_hash()
        for p, rec in recs:
            fresh = read_json(p, None)
            if not isinstance(fresh, dict) or fresh.get("state") != "prepared":
                continue                        # finished meanwhile
            ev = fresh.get("event") or {}
            if fresh.get("intended_hash") == cur:
                fresh["state"] = "committed"
                write_private(p, json.dumps(fresh))
                admin_log(f"{ev.get('type')} {ev.get('seq')}: its change was written; announcing it")
            else:
                remove_records(admin_dir(), [os.path.basename(p)], ADMIN_NAME)
                admin_log(f"{ev.get('type')} {ev.get('seq')}: its change was never written; dropped")


def _coalesced(evs):
    """One message for several announcements to the same phone."""
    last = evs[-1]
    ev = dict(last)
    ev.update(id=str(uuid.uuid4()), type="sink_changes",
              message=clean_str(f"{len(evs)} changes to your event sinks: " + "; ".join(e.get("message", "") for e in evs),
                                MAX["message"]),
              data={"changes": [{"type": e.get("type"), "seq": e.get("seq"), "id": e.get("id")} for e in evs]})
    return ev


def flush_admin(budget=EMIT_BUDGET, force=False):
    """Deliver journaled sink-change announcements to their old destinations, with backoff; give up on a
    target after ADMIN_MAX_ATTEMPTS (noted in events-state/admin.log and shown by `events status`). A phone
    gets them under its cap like anything else, several waiting at once going as one message."""
    try:
        recover_journal()
    except Exception as e:
        note_error(f"journal recovery: {e!r}")
    if not _admin_records():
        return
    try:
        lock = Lock(os.path.join(state_dir(), "admin.lock"), 0.5)
        lock.__enter__()
    except Busy:
        return
    try:
        recs = {p: r for p, r in _admin_records() if r.get("state", "committed") == "committed"}
        now, deadline, changed = time.time(), time.time() + budget, set()
        groups = {}
        for p, rec in recs.items():
            for name, t in rec["targets"].items():
                if t.get("state") != "pending" or (not force and float(t.get("next") or 0) > now):
                    continue
                groups.setdefault((name, dumps(t["sink"])), []).append((p, t))
        for (name, _), items in groups.items():
            if deadline - time.time() < 0.3:
                break
            sink = items[0][1]["sink"]
            if sink.get("kind") == "ntfy":
                room, free_at = phone_slot(name)
                if not room:                    # wait for the phone's next slot (not an attempt)
                    for p, t in items:
                        t["next"] = free_at
                        changed.add(p)
                    continue
                evs = [recs[p]["event"] for p, _ in items]
                units = [([evs[0]] if len(evs) == 1 else [_coalesced(evs)], items)]
            else:
                units = [([recs[p]["event"]], [(p, t)]) for p, t in items]
            for batch, members in units:
                left = deadline - time.time()
                if left < 0.3:
                    break
                try:
                    ok, err, permanent, rejected = deliver(sink, batch, min(TIMEOUT, left))
                except Exception as e:
                    ok, err, permanent, rejected = False, short_error(e), False, 0
                for p, t in members:
                    changed.add(p)
                    ev = recs[p]["event"]
                    if ok and rejected:
                        # An OTLP collector took the request but refused the announcement (partial success):
                        # it isn't stored there, and OTLP says not to retry it.
                        t["state"], t["error"] = "abandoned", err
                        admin_log(f"{ev.get('type')} {ev.get('seq')}: refused by {name} ({err})")
                        note_error(f"sink-change announcement {ev.get('seq')} to {name} refused ({err})")
                        continue
                    if ok:
                        t["state"] = "delivered"
                        admin_log(f"{ev.get('type')} {ev.get('seq')}: delivered to {name}"
                                  + (f" (with {len(members) - 1} more in one message)" if len(members) > 1 else ""))
                        continue
                    t["attempts"] = int(t.get("attempts") or 0) + 1
                    t["error"], t["next"] = err, time.time() + backoff(t["attempts"])
                    if t["attempts"] >= ADMIN_MAX_ATTEMPTS or (permanent and t["attempts"] >= 3):
                        t["state"] = "abandoned"
                        admin_log(f"{ev.get('type')} {ev.get('seq')}: gave up on {name} after {t['attempts']} attempts ({err})")
                        note_error(f"sink-change announcement {ev.get('seq')} to {name} abandoned ({err})")
        for p in changed:
            rec = recs[p]
            if all(t.get("state") != "pending" for t in rec["targets"].values()):
                remove_records(admin_dir(), [os.path.basename(p)], ADMIN_NAME)
            else:
                write_private(p, json.dumps(rec))
    finally:
        lock.__exit__()


def admin_pending():
    out = []
    for _, rec in _admin_records():
        for name, t in rec["targets"].items():
            if t.get("state") == "pending":
                out.append({"type": rec["event"].get("type"), "sink": name, "attempts": t.get("attempts"),
                            "error": t.get("error"), "state": rec.get("state", "committed")})
    return out


def _known_paths():
    return os.path.join(state_dir(), "config.sha256"), os.path.join(state_dir(), "config.good.json")


def remember_config(text):
    """The configuration as `events sink ...` last wrote it: its hash and a copy. Hold the global lock."""
    hp, gp = _known_paths()
    write_private(hp, hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest())
    write_private(gp, text if isinstance(text, str) else text.decode("utf-8", "replace"))


def sink_brief(name, s):
    return {"sink": name, "kind": (s or {}).get("kind"), "url": mask_url((s or {}).get("url") or ""),
            "families": (s or {}).get("families") or ["all"]}


def migrate_state():
    """Remove what earlier builds left that this one must not keep: f615a13 stored a verifier of the
    live-view password (its length and a 3-byte keyed HMAC) in events-state/redact.json."""
    p = os.path.join(state_dir(), "redact.json")
    if os.path.exists(p):
        try:
            os.remove(p)
            note_error("removed events-state/redact.json, a live-view password verifier an earlier build kept "
                       "(docs/events.md: change that password if it lives in your own secret store)")
        except OSError:
            pass


def _config_drift():
    """If events.json isn't what `events sink ...` last wrote: journal security.sink_config_changed to the
    sinks of the last known-good configuration FIRST, then take the new file as known (so a crash in between
    announces it again rather than never). A broken file is announced too (and its sinks aren't used).
    Hold the global lock. Returns True when it journaled an announcement."""
    migrate_state()
    hp, gp = _known_paths()
    try:
        raw = open(config_path(), "rb").read()
        cur = hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        raw, cur = None, "absent"
    try:
        known = open(hp).read().strip()
    except OSError:
        known = None
    if known is None:                       # first run with this version: take what's there as known
        if raw is not None:
            remember_config(raw)
        return False
    if known == cur:
        return False
    records = _admin_records()
    if any(r.get("intended_hash") == cur for _, r in records):
        write_private(hp, cur)              # our own change, written just before a crash: nothing to say
        return False
    good = read_json(gp, {})
    good_sinks = good.get("sinks") if isinstance(good, dict) and isinstance(good.get("sinks"), dict) else {}
    try:
        new = json.loads(raw) if raw is not None else {"sinks": {}}
        new_sinks = new.get("sinks") if isinstance(new, dict) and isinstance(new.get("sinks"), dict) else None
    except ValueError:
        new_sinks = None
    keys = ("kind", "url", "headers", "families", "min_severity", "ca_pem", "hmac_key", "generation")
    diff = {"readable": new_sinks is not None, "added": [], "removed": [], "changed": []}
    if new_sinks is not None:
        diff["added"] = [sink_brief(n, s) for n, s in new_sinks.items() if n not in good_sinks]
        diff["removed"] = [sink_brief(n, s) for n, s in good_sinks.items() if n not in new_sinks]
        diff["changed"] = [sink_brief(n, new_sinks[n]) for n in good_sinks if n in new_sinks and isinstance(new_sinks[n], dict)
                           and any(good_sinks[n].get(k) != new_sinks[n].get(k) for k in keys)]
    quiet = diff["readable"] and not (diff["added"] or diff["removed"] or diff["changed"])
    journaled = False
    if not quiet and not any(r.get("config_hash") == cur for _, r in records):
        parts = ([f"added {', '.join(d['sink'] for d in diff['added'])}"] if diff["added"] else []) + \
                ([f"removed {', '.join(d['sink'] for d in diff['removed'])}"] if diff["removed"] else []) + \
                ([f"changed {', '.join(d['sink'] for d in diff['changed'])}"] if diff["changed"] else [])
        what = "; ".join(parts) if diff["readable"] else "it can't be read, so nothing is sent until it's fixed"
        journal_announcement("sink_config_changed",
                             f"The event sinks were changed outside 'metacomputer events sink': {what}", diff,
                             good_sinks, {"sinks": good_sinks},
                             action="If you didn't do this, check ~/.config/metacomputer/events.json and "
                                    "metacomputer events sink list", config_hash=cur)
        journaled = True
    write_private(hp, cur)                  # now this version is the known one (announced, or nothing to say)
    if new_sinks is not None and raw is not None:
        write_private(gp, raw.decode("utf-8", "replace"))
    return journaled


def config_guard():
    """Notice events.json changed other than through `events sink ...`: tell the sinks of the last
    configuration it wrote (security.sink_config_changed). Best effort: a program running as you can also
    change what this compares against."""
    try:
        with global_lock():
            journaled = _config_drift()
    except Exception as e:
        note_error(f"config check: {e!r}")
        return
    if journaled:
        flush_admin(EMIT_BUDGET)


# --- emitting -------------------------------------------------------------------------------
def emit(family, type_, message, severity=None, action=None, data=None, vm=None, project=None,
         debounce=None, debounce_s=DEBOUNCE_S, deliver_now=True, only_sinks=None, budget=EMIT_BUDGET,
         raise_errors=False, origin="cli", exempt=False):
    """Record one event and try to deliver it. Best effort: returns the event, or None (debounced, not
    wanted, or a problem, which is noted for `events status`). Never raises unless raise_errors.
    only_sinks: exactly these sinks, whatever their filters. exempt: outside the family limit (code paths
    only: sink tests, the relay's actions events). Phone sinks keep their own cap either way."""
    try:
        if family == "actions" and only_sinks is None and not wants(load_config(), "actions"):
            return None
        ev = build_event(family, type_, message, severity, action, data, vm,
                         project if project is not None else _default_project(), origin=origin)
        return emit_built(ev, debounce, debounce_s, deliver_now, only_sinks, budget, exempt)
    except Exception as e:
        if raise_errors:
            raise
        note_error(f"emit {family}.{type_}: {e!r}")
        return None


def emit_built(ev, debounce=None, debounce_s=DEBOUNCE_S, deliver_now=True, only_sinks=None, budget=EMIT_BUDGET,
               exempt=False):
    """Record an event that build_event made (keeping its id), then try to deliver it."""
    config_guard()
    key = f"{ev['family']}.{ev['type']}:{debounce}" if debounce else None
    with global_lock():
        cfg = config_safe()                     # read under the lock: never queue to a retired generation
        if key and debounced(key, debounce_s):
            return None
        drain_spool(cfg)
        record(ev, cfg, only_sinks, exempt)
        if key:
            mark_debounce(key)                  # only once the event is safely recorded
    if deliver_now:
        flush_all(cfg, budget)
        flush_admin(budget)
    return ev


def _default_project():
    try:
        return os.getcwd()
    except OSError:
        return None


_async = {"q": None, "thread": None, "lock": threading.Lock(), "items": []}


def _claim(item, frm="new", to="taken"):
    with _async["lock"]:
        if item["state"] != frm:
            return False
        item["state"] = to
        return True


def _finish(item):
    with _async["lock"]:
        item["state"] = "done"
        if item in _async["items"]:
            _async["items"].remove(item)


def _record_item(item):
    try:
        emit_built(item["ev"], item["debounce"], item["debounce_s"], deliver_now=False, exempt=item["exempt"])
    except Exception as e:
        note_error(f"async emit: {e!r}")
    finally:
        _finish(item)


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
            for it in items:
                if _claim(it):
                    _record_item(it)
            flush_all(config_safe())
            flush_admin()
        except Exception as e:
            note_error(f"async emit: {e!r}")
        finally:
            for _ in items:
                q.task_done()


def emit_async(family, type_, message, severity=None, action=None, data=None, vm=None, project=None,
               debounce=None, debounce_s=DEBOUNCE_S, origin="bridge", exempt=False):
    """emit() for long-lived processes (the MCP bridges) that must never wait: the event is built here (so
    it has its id), and recorded and sent on a background thread. flush_async() before exiting records
    whatever that thread didn't get to."""
    try:
        if family == "actions" and not wants(config_for_reading(), "actions"):
            return None
        ev = build_event(family, type_, message, severity, action, data, vm,
                         project if project is not None else _default_project(), origin=origin)
    except Exception as e:
        note_error(f"async emit {family}.{type_}: {e!r}")
        return None
    item = {"ev": ev, "debounce": debounce, "debounce_s": debounce_s, "exempt": exempt, "state": "new"}
    with _async["lock"]:
        _async["items"].append(item)
        if _async["q"] is None:
            _async["q"] = queue.Queue()
            _async["thread"] = threading.Thread(target=_worker, daemon=True)
            _async["thread"].start()
    _async["q"].put(item)
    return ev


def flush_async(timeout=2.0):
    """Before a bridge exits: wait for its events to be recorded. Then record here those the worker hasn't
    started; and, after a short further wait, those it started but didn't finish (the same event, same id:
    at worst it arrives twice, never not at all). Sending is left to the watcher or the next emission."""
    def unfinished():
        with _async["lock"]:
            return [it for it in _async["items"] if it["state"] != "done"]
    end = time.monotonic() + timeout
    while unfinished() and time.monotonic() < end:
        time.sleep(0.05)
    for it in unfinished():
        if _claim(it):
            _record_item(it)
    end = time.monotonic() + 1.0
    while any(it["state"] == "taken" for it in unfinished()) and time.monotonic() < end:
        time.sleep(0.05)
    for it in unfinished():
        if _claim(it, "taken", "rescued"):
            _record_item(it)


def drain(force=False, budget=WATCH_BUDGET):
    config_guard()
    with global_lock():
        cfg = config_safe()
        n, bad = drain_spool(cfg)
        flush_suppressed(cfg)
    flush_all(cfg, budget, force)
    flush_admin(budget, force)
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

    def grew(self):
        """Whether the log changed since the last poll (a cheap stat, for the watcher's quick looks between
        its cycles). Compared with what the last poll saw, not where it got to, so a half-written last line
        isn't read again every few seconds."""
        try:
            s = os.stat(self.path)
        except OSError:
            return False
        return (s.st_ino, s.st_size) != getattr(self, "seen", (self.st.get("netgw_inode"), self.st.get("netgw_offset")))

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
        self.seen = (s.st_ino, s.st_size if len(data) < 4 * 2**20 else -1)   # -1: more to read, look again
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
        st["netgw_new"] = True                 # blocked_event decides whether these open a quiet spell's report
    st["netgw_pending"] = list(index.values())


def _num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else default


def blocked_event(st, vm=None, now=None):
    """The security event for the pending blocked connections, or None (then they wait).
    - The first blocked connection after NETGW_QUIET_S with none goes out at once (`first`), at most
      NETGW_FIRST_CAP times in any hour; the watcher looks at the gateway's log every few seconds.
    - Everything after it, until the next quiet spell, is batched: at most one event a minute, and when the
      same destinations come back each time, the wait doubles, up to an hour.
    The family limit and each phone's cap still apply on top (record(), phone_slot())."""
    pend = [e for e in (st.get("netgw_pending") or []) if isinstance(e, dict)]
    now = now or time.time()
    new = st.pop("netgw_new", False) is True
    seen_before = st.get("netgw_seen")
    if new:
        st["netgw_seen"] = now                 # the watcher's own clock, never the gateway's record times
    if not pend:
        return None
    firsts = [t for t in (st.get("netgw_firsts") or [])
              if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= now - t < NETGW_FIRST_CAP[1]]
    quiet = new and (seen_before is None or now - _num(seen_before) >= NETGW_QUIET_S)
    first = quiet and len(firsts) < NETGW_FIRST_CAP[0]
    sig = hashlib.sha256(dumps(sorted([str(e.get("host")), str(e.get("port")), str(e.get("proto")),
                                       str(e.get("reason"))] for e in pend)).encode()).hexdigest()[:16]
    same = sig == st.get("netgw_sig")
    repeat = int(_num(st.get("netgw_repeat"))) if same else 0
    wait = min(NETGW_BATCH_S * 2 ** min(repeat + 1, 10), NETGW_MAX_S) if same else NETGW_BATCH_S
    st["netgw_firsts"] = firsts
    if not first and now - _num(st.get("netgw_last")) < wait:
        return None
    if first:
        firsts.append(now)
    dests = sorted(pend, key=lambda e: -int(e.get("count") or 0))
    total = sum(int(d.get("count") or 0) for d in dests)
    top = ", ".join(f"{safe_host(d.get('host'))}{':' + str(d['port']) if d.get('port') else ''} ({safe_word(d.get('reason'))})"
                    + (f" x{d['count']}" if d["count"] > 1 else "") for d in dests[:3])
    st["netgw_pending"], st["netgw_last"], st["netgw_sig"] = [], now, sig
    st["netgw_repeat"] = repeat + 1 if same else 0
    return {"family": "security", "type": "blocked_connections", "severity": "warn", "vm": vm,
            "message": f"The VM tried {total} connection(s) its gateway blocks: {top}"
                       + (f" and {len(dests) - 3} more destination(s)" if len(dests) > 3 else ""),
            "data": {"count": total, "destinations": dests[:20], "log": "logs/netgw-blocked.jsonl",
                     "same_as_before": same, "first": first}}


IP_RE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:.]{2,45})$")


def safe_host(h):
    """A destination as it may appear in a message: an IP address, else just 'a destination' (the gateway
    writes addresses; anything else stays in data, cleaned)."""
    return h if isinstance(h, str) and IP_RE.fullmatch(h) else "a destination"


def safe_word(w):
    """A gateway reason as it may appear in a message: letters, digits and :_- only."""
    return w if isinstance(w, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", w) else "blocked"


HEALTH_TEXT = {
    "vm_off": ("error", "The virtual computer is not running (state: {vm}).", "metacomputer start, or metacomputer status"),
    "gateway": ("error", "The VM's network gateway (mcomp-netgw) is down.", "metacomputer restart"),
    "ssh": ("error", "The VM is running but doesn't answer SSH.", "metacomputer status; metacomputer restart if it stays down"),
    "not_ready": ("warn", "The VM's desktop session isn't ready (Xorg, XFCE or accessibility).", "metacomputer status"),
    "doctor": ("warn", "Cua Driver's doctor check is failing, or didn't report a result.", "metacomputer status"),
    "disk_low": ("warn", "Less than 3 GB free on the VM's disk.", "free space in the VM (metacomputer inspect files), or reset it if you agree"),
    "recording_failed": ("error", "The desktop recording '{rec}' stopped on its own.", "metacomputer record stop, to save what it captured"),
}


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def guest_health(gs):
    """guest-status's fields (from `metacomputer _health`) as fixed categories: the doctor's result 'ok', 'failed',
    'unknown' or None (not reported), free disk 'ok', 'low' (under 3 GB) or 'unknown'. The VM chooses every value
    there, and one that holds a value from the vault could spell it in a number or a string, so nothing it chose
    reaches a message or data: only these words. Takes the categories `_health` sends, or guest-status's own fields."""
    gs = gs if isinstance(gs, dict) else {}
    doctor = gs.get("driver_doctor")
    if doctor not in ("ok", "failed", "unknown", None):
        doctor = "unknown"
    if "driver_doctor" not in gs and "driver_doctor_rc" in gs:
        rc = gs.get("driver_doctor_rc")
        is_int = isinstance(rc, int) and not isinstance(rc, bool)
        doctor = None if rc is None else ("ok" if is_int and rc == 0 else "failed" if is_int else "unknown")
    disk = gs.get("disk")
    if "disk" not in gs:
        v = gs.get("disk_free_gb")
        disk = "unknown" if not _number(v) or v < 0 else "low" if v < 3 else "ok"
    elif disk not in ("ok", "low", "unknown"):
        disk = "unknown"
    return {"ready": gs.get("ready") is True, "doctor": doctor, "disk": disk}


def health_conditions(h):
    """Problems in a `metacomputer _health` answer: {condition: (severity, message, action)}.
    Nothing that comes from inside the VM reaches a message: its report becomes fixed categories (guest_health)."""
    out = {}
    fmt = {"vm": str(h.get("vm"))[:32], "rec": str((h.get("recording") or {}).get("name"))[:64]}

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
            g = guest_health(h.get("guest"))
            if not g["ready"]:
                put("not_ready")
            if g["doctor"] in ("failed", "unknown"):
                put("doctor")
            if g["disk"] == "low":
                put("disk_low")
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


def emit_dict(e, project=None, origin="cli", deliver_now=False):
    return emit(e["family"], e["type"], e["message"], e.get("severity"), e.get("action"), e.get("data"),
                e.get("vm"), project, deliver_now=deliver_now, origin=origin)


def blocked_step(st, tail, vm, deliver_now=False, lock=None):
    """New gateway records into the pending counts, and the event they make now, if any (blocked_event).
    lock: held while the state is read and changed and the event recorded (the watcher's own thread for the gateway's
    log shares the state with its cycle). deliver_now: then send it at once, outside the lock (the watcher's poller),
    not at the cycle's delivery step."""
    with (lock if lock is not None else _NoLock()):
        add_blocked(st, tail.poll())
        ev = blocked_event(st, vm)
        if ev:
            emit_dict(ev, origin="watcher")
    # A first report goes out now whichever reader recorded it: the cycle's own read would otherwise leave it queued
    # behind the cycle's health probe and deliveries, and the poller wouldn't see it again (the log didn't grow).
    if ev and (deliver_now or (ev.get("data") or {}).get("first") is True):
        flush_all(config_safe())
    return ev


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def watch_cycle(st, tail, vm, health=False, lock=None):
    """One cycle. Each step is on its own, so a problem in one (an odd gateway record, a failed probe)
    doesn't stop the others. lock: held while the shared state changes (never during the probe or deliveries)."""
    lk = lock if lock is not None else _NoLock()

    def step(name, fn):
        try:
            fn()
        except Exception as e:
            note_error(f"watcher {name}: {e!r}")

    def spool():
        with global_lock():
            cfg = config_safe()
            drain_spool(cfg)
            flush_suppressed(cfg)

    def blocked():
        blocked_step(st, tail, vm, lock=lock)

    def probe():
        with lk:
            st["health_last"] = time.time()
        h = probe_health()                      # up to minutes: the gateway's log is watched meanwhile (watch())
        with lk:
            evs = health_events(st, h, vm)
        for e in evs:
            emit_dict(e, origin="watcher")
    step("configuration", config_guard)
    step("spool", spool)
    step("blocked connections", blocked)
    if health:
        step("health", probe)
    step("delivery", lambda: flush_all(config_safe(), WATCH_BUDGET))
    step("sink-change announcements", lambda: flush_admin(WATCH_BUDGET))


def needs_watcher():
    """A watcher is wanted while there are sinks, or sink-change announcements still to deliver (removing the
    last sink mustn't stop its own announcement's retries), or events.json can't be read."""
    try:
        if load_config().get("sinks"):
            return True
    except Exception:
        return True
    return bool(admin_pending())


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
        sfd = _open_dir(spool_dir())             # the spool it consumes, checked as it starts (and before each sweep)
        if sfd is not None:
            try:
                why = spool_guard(sfd)
            finally:
                os.close(sfd)
            if why:
                note_once(f"spool-guard:{why}", f"spool {spool_dir()} isn't consumed: {why}")
                print(f"the spool {spool_dir()} won't be consumed: {why}", flush=True)
    sp = watch_state_path(vm)
    st = read_json(sp, {})
    st = st if isinstance(st, dict) else {}
    if not once or "health_last" not in st:
        st["health_last"] = time.time()           # the first probe 5 minutes after start
    tail = NetgwTail(os.path.join(log_dir(), "netgw-blocked.jsonl"), st)
    lk = threading.Lock()                       # the state, the gateway log's place, recording a blocked report

    def save():
        with lk:
            write_private(sp, json.dumps(st))   # where it got to, so nothing is reported twice

    def poll_blocked():
        """The gateway's log, on a thread of its own: every NETGW_POLL_S seconds, whatever the cycle is doing (a health
        probe or a slow sink can take minutes), so the first blocked connection after a quiet spell goes out within
        seconds. blocked_event decides; follow-ups stay batched."""
        while not stop["now"]:
            for _ in range(NETGW_POLL_S * 4):
                if stop["now"]:
                    return
                time.sleep(0.25)
            try:
                with lk:
                    grown = tail.grew()
                if grown:
                    blocked_step(st, tail, vm, deliver_now=True, lock=lk)
                    save()
            except Exception as e:
                note_error(f"watcher blocked connections (poller): {e!r}")
    poller = None
    if not once:
        poller = threading.Thread(target=poll_blocked, name="netgw-poller", daemon=True)
        poller.start()
    try:
        while not stop["now"]:
            due = health or (not once and time.time() - float(st.get("health_last") or 0) >= HEALTH_INTERVAL)
            watch_cycle(st, tail, vm, health=due, lock=lk)
            try:
                save()
            except Exception as e:
                note_error(f"watcher state: {e!r}")
            if once:
                break
            if not needs_watcher():
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} no sinks and no announcement to deliver; watcher exits",
                      flush=True)
                break
            for _ in range(WATCH_INTERVAL):
                if stop["now"]:
                    break
                time.sleep(1)
    finally:
        stop["now"] = True
        if poller is not None:
            poller.join(10)                     # let a report it's recording reach the state first
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
    tool = re.sub(r"[^A-Za-z0-9_.-]", "", str(rec.get("tool") or ""))[:40] or None
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
    typ = t if TYPE_RE.fullmatch(str(t)) else "record"
    return {"family": "actions", "type": typ, "severity": "warn" if t in ("refused", "sign_failed") else "info",
            "message": msg, "data": data}


SIZE_BOUNDS = (0, 1 << 10, 1 << 16, 1 << 20, 1 << 24, 1 << 28, 1 << 32, 1 << 40)


def size_bound(n):
    """A size the VM can steer (a video it made or sent), as the smallest of 0, 1 KiB, 64 KiB, 1 MiB, 16 MiB,
    256 MiB, 4 GiB and 1 TiB that holds it (as the vault's sizeBound): an exact count could spell a value the VM
    was given; one of eight values carries three bits at most. None when it isn't a size."""
    if not _number(n) or n < 0:
        return None
    return next((b for b in SIZE_BOUNDS if n <= b), SIZE_BOUNDS[-1])


def parse_ts(ts):
    """A description file's local time ("%Y-%m-%dT%H:%M:%S%z", written on this Mac) as epoch seconds, or None."""
    from datetime import datetime
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp() if isinstance(ts, str) else None
    except ValueError:
        return None


def count_class(n):
    """A count the VM can steer (folders it made, files it changed), as one of a few words (the vault's countClass)."""
    if not _number(n) or n <= 0:
        return "none"
    return "1-9" if n < 10 else "10-99" if n < 100 else "100 or more"


def recording_event(mp4):
    """task.recording_stopped from a desktop recording and its description file. The video's content, and so its
    size and its own idea of its length, is the VM's: the event carries the length as this Mac's clock measured it
    (record start to record stop) and the size only as a class, never the numbers the file holds."""
    side = read_json(mp4[:-4] + ".json" if mp4.endswith(".mp4") else mp4 + ".json", {})
    a, b = parse_ts(side.get("started_at")), parse_ts(side.get("ended_at"))
    dur = round(b - a) if a is not None and b is not None and b >= a else None
    name = side.get("name") or os.path.basename(mp4)[:-4]
    early = bool(side.get("stream_ended_early"))    # the VM stopped sending before record stop
    return {"family": "task", "type": "recording_stopped", "severity": "warn" if early else "info",
            "message": f"Recording '{name}' saved" + (f" ({dur} s)" if dur is not None else "")
            + ("; the VM stopped sending before record stop" if early else ""),
            "data": {"name": name, "duration_s": dur, "path": mp4,
                     "description_file": (mp4[:-4] + ".json") if side else None,
                     "key_moments": len(side.get("moments") or []), "audio": bool(side.get("audio")),
                     "mode": side.get("mode") if side.get("mode") in ("host", "guest") else None,
                     "size_bound": size_bound(side.get("bytes")), "ended_early": early}}


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
    if not sep or not HEADER_NAME_RE.fullmatch(name) or not value or any(c in value for c in "\r\n\0"):
        raise EventsError("--header takes 'Name: value'")
    return [name, value]


def parse_header_env(h):
    name, sep, tmpl = h.partition("=")
    name = name.strip()
    if not sep or not HEADER_NAME_RE.fullmatch(name) or not tmpl.strip():
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
    st = load_status(name, gen_of(sink))
    waiting = backlog(name, gen_of(sink), st)
    return {"name": name, "kind": sink["kind"], "url": mask_url(sink["url"]),
            "families": sink.get("families") or ["all"], "min_severity": sink.get("min_severity") or "info",
            "headers": [h[0] for h in sink.get("headers") or []], "ca": bool(sink.get("ca_pem")),
            "signed": sink["kind"] == "webhook", "created": sink.get("created"), "backlog": waiting,
            "delivered": st["delivered"], "dropped": st["dropped"], "last_ok_at": st["last_ok_at"],
            "rejected": int(st.get("rejected") or 0),
            # a failure, a backlog, or a receiver's refusal (OTLP partial success) since its last full success
            "last_error": st["last_error"] if st["failures"] or waiting or (
                st.get("last_rejection_at") and (st["last_error_at"] or 0) >= (st["last_ok_at"] or 0)) else None,
            "last_error_at": st["last_error_at"], "next_retry_in": max(0, int(st["next_retry"] - time.time()))}


def confirm_change(a, question):
    """Adding, replacing or removing a sink decides what leaves this Mac: only from a terminal, so a program
    without one (an agent's shell, a script) can't do it quietly. Friction, not a lock: a program running as
    you can fake a terminal; the announcement and config_guard are what tell you."""
    if not sys.stdin.isatty():
        raise EventsError(f"changing event sinks decides what leaves this Mac, so it needs your own terminal: run "
                          f"`metacomputer events sink {a.sub} ...` there (add --yes to skip the question)")
    if a.yes:
        return
    sys.stderr.write(question + " [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        raise EventsError("cancelled")


def remove_sink_files(name, gen):
    for ext in ("queue", "status.json", "appended"):
        try:
            os.remove(sink_file(name, gen, ext))
        except OSError:
            pass


def change_sink(name, new_sink, expect, project=None):
    """Put `new_sink` (a new generation) in place of `name`, or remove it (None), as one transaction under
    the sink's own lock (nothing is sending its backlog) and the global lock:
    - `expect` is the generation the user confirmed (None: the name didn't exist); if the sinks changed
      since, the change is refused rather than applied to something nobody looked at;
    - the sinks that exist now are the announcement's targets, captured here;
    - the announcement is journaled ('prepared') before the file is written and committed after, so an
      interruption either announces what was written or drops what wasn't (recover_journal).
    Returns (the announcement event, the names of the sinks it goes to)."""
    try:
        lock = Lock(sink_lock(name), 30)
        lock.__enter__()
    except Busy:
        raise EventsError(f"sink {name} is busy sending; try again in a moment")
    try:
        with global_lock():
            _config_drift()                     # a hand edit before this gets its own announcement first
            try:
                cfg = load_config()
            except Exception as e:
                raise EventsError(f"events.json can't be read ({e}); fix it, or move it away, first")
            old = cfg["sinks"].get(name)
            if (gen_of(old) if isinstance(old, dict) else None) != expect:
                raise EventsError("the event sinks changed while you were answering; run the command again")
            prior = {n: s for n, s in cfg["sinks"].items() if isinstance(s, dict)}
            subject = new_sink if new_sink is not None else old
            if new_sink is None:
                cfg["sinks"].pop(name, None)
            else:
                cfg["sinks"][name] = new_sink
            text = render_config(cfg)
            type_ = "sink_removed" if new_sink is None else "sink_added"
            data = {**sink_brief(name, subject), "min_severity": subject.get("min_severity") or "info",
                    "from_terminal": True, "replaced": old is not None and new_sink is not None}
            verb = "removed" if new_sink is None else ("replaced" if data["replaced"] else "added")
            ev, path = journal_announcement(
                type_, f"Event sink '{name}' {verb}: {data['kind']} to {data['url']}, families "
                       f"{','.join(data['families'])}", data, prior, {"sinks": prior}, project=project,
                state="prepared", intended_hash=hashlib.sha256(text.encode()).hexdigest())
            save_config(cfg, text)
            if path:
                rec = read_json(path, {})
                rec["state"] = "committed"
                write_private(path, json.dumps(rec))
            if new_sink is not None:
                save_status(name, gen_of(new_sink), dict(STATUS0, sent_seq=current_seq()))
        if old is not None and (new_sink is None or gen_of(old) != gen_of(new_sink)):
            remove_sink_files(name, gen_of(old))
    finally:
        lock.__exit__()
    flush_admin(EMIT_BUDGET)
    return ev, sorted(prior)


def cmd_sink(a):
    cfg = load_config()
    sinks = cfg["sinks"]
    if a.sub == "add":
        if not NAME_RE.fullmatch(a.name):
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
                "families": parse_families(a.families), "min_severity": a.min_severity, "created": now_iso(),
                "generation": secrets.token_hex(8)}
        if a.kind == "webhook":
            sink["hmac_key"] = base64.b64encode(secrets.token_bytes(32)).decode()
        old = sinks.get(a.name)
        replacing = old is not None
        dropped = backlog(a.name, gen_of(old)) if replacing else 0
        confirm_change(a, f"{'Replace' if replacing else 'Add'} event sink '{a.name}': {a.kind} to {mask_url(a.url)}, "
                          f"families {','.join(sink['families'])}, from {a.min_severity}?")
        _, prior = change_sink(a.name, sink, gen_of(old) if replacing else None, caller_project())
        _known["at"] = 0
        fams = ",".join(sink["families"])
        print(f"{'replaced' if replacing else 'added'} sink {a.name}: {a.kind} to {mask_url(a.url)}; families {fams}, "
              f"from {a.min_severity}" + (f"; headers {', '.join(h[0] for h in headers)} (values hidden)" if headers else ""))
        if replacing:
            print(f"  the old {a.name}'s signing key no longer works, and {dropped} event(s) still waiting for it "
                  "were dropped")
        if prior:
            print(f"  told the sinks you had ({', '.join(prior)}): security.sink_added")
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
            if r["rejected"]:
                print(f"{'':14} refused by the receiver in accepted requests (OTLP partial success; dropped, "
                      f"not retried): {r['rejected']} event(s)")
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
                          f"{backlog(a.name, gen_of(sink))} event(s) waiting for it are dropped.")
        _, prior = change_sink(a.name, None, gen_of(sink), caller_project())     # the removed sink is told too
        print(f"removed sink {a.name}; told the sinks you had ({', '.join(prior)}): security.sink_removed")
        return 0
    if a.sub == "test":
        ev = emit("lifecycle", "test", f"Test event for sink '{a.name}' from metacomputer on {host_name()}",
                  only_sinks={a.name}, deliver_now=False, project=caller_project(), raise_errors=True, exempt=True)
        gen = gen_of(sink)
        end, st = time.time() + 60, load_status(a.name, gen)
        while time.time() < end and st["sent_seq"] < ev["seq"]:
            before = st["sent_seq"]
            st = flush_sink(a.name, sink, budget=10, force=True, wait_lock=5) or load_status(a.name, gen)
            if st["sent_seq"] == before:
                break                          # no progress: the sink refused or is unreachable
        if st["sent_seq"] >= ev["seq"]:
            print(f"OK: {a.name} accepted test event {ev['seq']}")
            return 0
        if st.get("waiting") == "phone limit":
            print(f"NOT YET: {a.name}'s phone limit is reached; {backlog(a.name, gen, st)} event(s) wait their turn "
                  f"(the test event among them), the next in {max(0, int(st['next_retry'] - time.time()))} s")
            return 1
        if st["sent_seq"] > 0 and not st["failures"]:
            print(f"NOT YET: {a.name} is still taking {backlog(a.name, gen, st)} earlier event(s); the test event "
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
        st = load_status(name, gen_of(sink))
        if ev.get("rate_limited"):
            out.append((name, "held back (rate limit)"))
            continue
        out.append((name, "delivered" if st["sent_seq"] >= ev["seq"] else
                    f"queued ({st['last_error'] or 'retried later'})"))
    return out


def content_key(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:16]


def cmd_emit(a, family, type_, message, severity, action, data, origin):
    """`events emit`, for scripts and metacomputer's shell hooks alike (origin script: anything that can run
    a command can call it, so a phone shows it as sent from the command line). No reserved types."""
    if reserved(family, type_):
        raise EventsError(f"{family}.{type_} is reserved for metacomputer's own code paths")
    key = content_key(family, type_, message, action, json.dumps(data, sort_keys=True, default=str))
    ev = emit(family, type_, message, severity, action, data, project=caller_project(), raise_errors=True,
              debounce=key if a.debounce > 0 else None, debounce_s=a.debounce, origin=origin)
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
              debounce_s=NOTIFY_DUP_S, origin="claude")
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
           "watcher": {"running": False, "pid": None}, "backlog": 0, "failing": [], "spool_waiting": 0, "errors": [],
           "announcements_pending": admin_pending()}
    try:
        cfg = load_config()
    except Exception as e:
        out["config_error"] = f"events.json can't be read ({e}); nothing is sent until it's fixed"
        out["errors"].append(out["config_error"])
        return out
    out["sinks"] = len(cfg["sinks"])
    out["names"] = sorted(cfg["sinks"])
    for n, s in sorted(cfg["sinks"].items()):
        st = load_status(n, gen_of(s))
        b = backlog(n, gen_of(s), st)
        out["backlog"] += b
        if b and st["last_error"]:
            out["failing"].append({"sink": n, "error": st["last_error"], "backlog": b})
        elif st.get("last_rejection_at") and time.time() - float(st["last_rejection_at"]) < 86400:
            # Delivered, but the receiver refused some of it (OTLP partial success): those were dropped.
            out.setdefault("rejected", []).append({"sink": n, "error": st["last_error"], "count": int(st.get("rejected") or 0)})
    pid = watcher_pid()
    out["watcher"] = {"running": bool(pid), "pid": pid}
    try:
        out["spool_waiting"] = sum(1 for f in os.listdir(spool_dir()) if f.endswith(".json") and not f.startswith("."))
    except OSError:
        pass
    out["spool_rejected"] = int((read_json(os.path.join(state_dir(), "spool.json"), {}) or {}).get("rejected", 0))
    out["rate_limited"] = {}
    for path, label in ((family_window(), ""), (phone_window(), "phone ")):
        w = read_json(path, {})
        for k, b in (w.items() if isinstance(w, dict) else []):
            if isinstance(b, dict) and int(b.get("held") or 0):
                out["rate_limited"][label + k] = int(b["held"])
    try:
        out["errors"] = open(os.path.join(state_dir(), "errors.log")).read().splitlines()[-3:]
    except OSError:
        pass
    try:
        out["announcements_log"] = open(os.path.join(state_dir(), "admin.log")).read().splitlines()[-3:]
    except OSError:
        out["announcements_log"] = []
    return out


def status_line(s):
    if s.get("config_error"):
        return s["config_error"] + (f"; {len(s['announcements_pending'])} sink-change announcement(s) still being "
                                    "delivered" if s.get("announcements_pending") else "")
    if not s["sinks"]:
        return "off (no sinks; metacomputer events sink add)"
    line = (f"{s['sinks']} sink{'s' if s['sinks'] != 1 else ''}, watcher "
            f"{'running' if s['watcher']['running'] else 'not running'}, backlog {s['backlog']}")
    if s["failing"]:
        line += "; failing: " + ", ".join(f"{f['sink']} ({f['error']})" for f in s["failing"])
    if s.get("rejected"):
        line += "; refused by the receiver in the last day (dropped): " + ", ".join(
            f"{f['sink']} ({f['count']}: {f['error']})" for f in s["rejected"])
    if s.get("rate_limited"):
        line += "; held back by the limits: " + ", ".join(f"{f} {n}" for f, n in s["rate_limited"].items())
    if s.get("announcements_pending"):
        line += f"; {len(s['announcements_pending'])} sink-change announcement(s) still being delivered"
    return line


def _emit_parser(sub, name):
    p = sub.add_parser(name)
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
    return p


def main(argv):
    ap = strictargs.Parser(prog="metacomputer events", description="Optional event stream (docs/events.md).")
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
    _emit_parser(sub, "emit")
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
    sub.add_parser("needs-watcher")
    p = sub.add_parser("_recording")
    p.add_argument("mp4")
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
            return cmd_emit(a, a.family, a.type, a.message, a.severity, a.action, data, "script")
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
            print(f"spool: {n} event(s) taken" + (f", {bad} invalid file(s) quarantined" if bad else "")
                  + "; " + status_line(status_info()))
            return 0
        if a.cmd == "watch":
            return watch(once=a.once, health=a.health)
        if a.cmd == "has-sinks":
            return 0 if load_config().get("sinks") else 1
        if a.cmd == "needs-watcher":
            return 0 if needs_watcher() else 1
        if a.cmd == "_recording":
            emit_dict(recording_event(a.mp4), caller_project())
            flush_all(load_config())
            return 0
    except (EventsError, ValueError, OSError, Busy) as e:
        print(f"metacomputer events: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
