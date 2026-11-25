#!/usr/bin/env python3
"""The optional event stream (lib/events.py, docs/events.md), with no VM.

    python3 tests/test_events.py

Runs local receivers for every kind (an HTTP server for webhook, OTLP and ntfy; a
TLS syslog server with its own CA, made with openssl) and checks:
- adding, listing, showing and removing sinks; private config; header values never printed;
- several sinks receiving the same events in order, and the four formats (CloudEvents with an
  HMAC signature, OTLP/HTTP JSON, RFC 5424 over TLS with octet counting, ntfy);
- filters by family and minimum severity; the opt-in `actions` family;
- cleaning: control characters, URL queries and credentials, secret-looking keys, sizes,
  header values and the live-view password;
- the spool; the backlog and retry when a receiver is down; a sink that never answers;
- ordering when several processes emit at once; debounce; `notify`;
- the watcher's pieces (blocked connections, health), the bridge's hooks, the relay's
  `actions` events, `actions verify` failures and `web open-untrusted` findings.
Everything lives in a temporary folder; nothing is sent off this Mac.
"""
import ast
import base64
import hashlib
import hmac
import json
import os
import pty
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD = tempfile.mkdtemp(prefix="mcomp-events-test-")
ENV = {"MCOMP_EVENTS_DIR": os.path.join(TD, "cfg"), "MCOMP_EVENTS_SPOOL": os.path.join(TD, "spool"),
       "MCOMP_LOG_DIR": os.path.join(TD, "logs"), "MCOMP_RUN_DIR": os.path.join(TD, "run"),
       "MCOMP_VM_NAME": "testvm", "MCOMP_SSH_DIR": os.path.join(TD, "ssh"), "MCOMP_DRIVER_MODE": "standard",
       "MCOMP_SESSIONS_DIR": os.path.join(TD, "sessions")}
os.environ.update(ENV)
for k in ("MCOMP_ACTION_LOG", "MCOMP_CONFIG"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, os.path.join(LAB, "tests"))
import events  # noqa: E402
import hostile_manifests  # noqa: E402  (every manifest value that reaches a generated file, tried with what breaks out)

EV = os.path.join(LAB, "lib", "events.py")
PY = sys.executable
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)


def cli(*args, env=None, timeout=60):
    return subprocess.run([PY, EV, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, **(env or {})))


def tty_cli(*args, answer=None, env=None, timeout=60):
    """The CLI with a terminal on stdin (sink changes need one); `answer` is typed in advance."""
    m, s_ = pty.openpty()
    try:
        if answer is not None:
            os.write(m, answer.encode())
        return subprocess.run([PY, EV, *args], stdin=s_, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, **(env or {})))
    finally:
        os.close(s_)
        os.close(m)


def sink_add(*args, **kw):
    return tty_cli("sink", "add", *args, "--yes", **kw)


def sink_remove(name, **kw):
    return tty_cli("sink", "remove", name, "--yes", **kw)


def refill():
    """Fresh rate-limit windows (families and phones), so one section's events don't hold back the next one's."""
    for f in ("ratelimit.json", "phonelimit.json"):
        try:
            os.remove(os.path.join(events.state_dir(), f))
        except OSError:
            pass


def stat_of(name):
    """A sink's delivery status (its current generation)."""
    return events.load_status(name, events.gen_of(events.load_config()["sinks"].get(name)))


def age_window(fname, key, seconds):
    """Move a sliding window's recorded times back, as if `seconds` had passed."""
    path = os.path.join(events.state_dir(), fname)
    w = json.load(open(path))
    if key in w:
        w[key]["times"] = [t - seconds for t in w[key].get("times") or []]
    events.write_private(path, json.dumps(w))


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def local_log():
    try:
        return [json.loads(x) for x in open(os.path.join(ENV["MCOMP_LOG_DIR"], "events.jsonl"))]
    except OSError:
        return []


# --- receivers -----------------------------------------------------------------------
SINK_CHANGE = (b'sink_added', b'sink_removed', b'sink_config_changed')


class HTTPRx:
    """Records every POST; `mode` = ok | 503 | 400, or a function of the body returning a status;
    `delay` seconds per request. Announcements of sink changes are kept apart, in `changes`."""

    def __init__(self):
        self.reqs, self.changes, self.mode, self.delay, rx = [], [], "ok", 0, self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(rx.delay)
                code = rx.mode(body) if callable(rx.mode) else {"ok": 200, "503": 503, "400": 400}[rx.mode]
                if code == 200:
                    req = (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                    is_change = any(b'"type":"' + t + b'"' in body or b'"stringValue":"' + t + b'"' in body
                                    or t.replace(b"_", b" ") in body for t in SINK_CHANGE)
                    (rx.changes if is_change else rx.reqs).append(req)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}{path}"

    def bodies(self):
        return [json.loads(b) for _, _, b in self.reqs]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def make_certs(d):
    """A throwaway CA and a server certificate for localhost, or None without openssl."""
    if not shutil.which("openssl"):
        return None
    os.makedirs(d, exist_ok=True)

    def run(*a):
        subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key",
        "-out", "ca.pem", "-days", "2", "-subj", "/CN=metacomputer test CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "srv.key",
        "-out", "srv.csr", "-subj", "/CN=localhost")
    with open(os.path.join(d, "ext"), "w") as f:
        f.write("subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
        "-days", "2", "-extfile", "ext")
    return os.path.join(d, "ca.pem"), os.path.join(d, "srv.pem"), os.path.join(d, "srv.key")


class SyslogRx:
    """RFC 5425: TLS, then octet-counted RFC 5424 messages. `mode` "ok" closes politely (close_notify back);
    "reset" resets the connection once the client has finished sending."""

    def __init__(self, cert, key):
        self.frames, self.changes, self.mode = [], [], "ok"
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        buf = b""
        try:
            t = self.ctx.wrap_socket(c, server_side=True)
            t.settimeout(5)
            while True:
                try:
                    d = t.recv(65536)
                except (ssl.SSLError, OSError):
                    break
                if not d:
                    break
                buf += d
            try:
                if self.mode == "reset":
                    t.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                else:
                    t.settimeout(1)
                    t.unwrap()
            except (ssl.SSLError, OSError, ValueError):
                pass
            try:
                t.close()
            except OSError:
                pass
        except (ssl.SSLError, OSError):
            return
        while buf:
            m = re.match(rb"(\d+) ", buf)
            if not m:
                break
            n = int(m.group(1))
            frame = buf[m.end():m.end() + n].decode()
            (self.changes if " sink_added " in frame or " sink_removed " in frame else self.frames).append(frame)
            buf = buf[m.end() + n:]


def parse_5424(frame):
    m = re.match(r'<(\d+)>1 (\S+) (\S+) (\S+) (\S+) (\S+) (\[[^\]]*(?:\\\][^\]]*)*\]) (.*)$', frame, re.S)
    if not m:
        return None
    pri, ts, host, app, procid, msgid, sd, msg = m.groups()
    return {"pri": int(pri), "ts": ts, "host": host, "app": app, "procid": procid, "msgid": msgid, "sd": sd,
            "msg": json.loads(msg)}


# --- the sinks' cases ----------------------------------------------------------------------------
def sink_cases():
    """The cases the vault's audit sinks are checked against too (tests/sink_cases.py; tests/test_vault_ops.py runs
    them through the vault): the stream's code answers each as it says."""
    print("the sinks' cases (the vault's copy of the same rules is checked against them)")
    import sink_cases as sc
    cs = sc.cases()
    shapes = [b for c in cs for b in sc.check_shape(c)]
    check("every case has its form (a rule of its folder, the answer, and a reason for each declared difference)",
          cs and not shapes, shapes[:6])
    wrong = []
    for c in cs:
        try:
            got = sc.stream(c)
        except Exception as e:
            got = f"raised {e!r}"
        if got != c["out"]:
            wrong.append((c["where"], c["in"], got, c["out"]))
    from sinkkit import manifests
    kinds = {c["kind"] for c in cs}
    need = {""} | {k for k in events.KINDS if manifests.manifest(k).get("vault")}      # the shared ones, and the vault's
    check(f"the stream's sinks answer all {len(cs)} cases as they say (header names, each kind's URLs, formats' numbers, "
          "fields and endpoints); every kind the vault's audit sinks have has its cases", not wrong and need <= kinds,
          (wrong[:6], sorted(need - kinds)))


# --- the sinks' folders, and no kind named in shared code ---------------------------------------
# Shared code: everything that runs on this Mac but the framework (lib/sinkkit) and the kinds' own folders. Not the vault
# (vault/, Swift: it keeps its own audit sinks, reading what they share with the stream from the manifests, compiled in by
# scripts/build-vault.sh; tests/sink_cases.py holds its parity) nor the docs (they name the kinds for people).
SINK_SHARED = ("bin", "scripts", "lib", "install.sh", "get.sh", "bench", "providers", "coding-agents", "tools")
# Names a kind's format is known by that its manifest doesn't hold as a word of its own.
SINK_EXTRA = {"webhook": ("CloudEvent",), "otlp": ("OpenTelemetry",)}
# Whole files allowed, each for its reason.
SINK_LINT_FILES = {
    "scripts/verify-audit.py": "the vault's audit-capture checker: it reads what the vault's audit sinks send (each "
                               "kind's wrapping, which stays the vault's own Swift), and its usage and messages name "
                               "them; its SD-ID comes from the syslog manifest",
}
# Lines allowed, by file and a piece of the line, each for its reason. An entry that matches nothing fails.
SINK_LINT_ALLOW = [
    ("bin/metacomputer", "#                                  the optional event stream: ",
     "`metacomputer help`'s line for the event stream (its header, printed as it is): made from the manifests' help "
     "words by scripts/gen-sinks.py"),
]


def sink_words():
    """{kind: a pattern for the words that name it}: its folder's name and its label in any case, a plural too, and
    SINK_EXTRA, each as a whole word (a letter or digit next to it makes it another word; an underscore, a hyphen, a
    dot or a + doesn't, so otlp_url, syslog+tls and ntfy.sh name theirs)."""
    from sinkkit import manifests, registry
    out = {}
    for k in registry.SINKS:
        ws = {k, manifests.manifest(k)["label"]} | set(SINK_EXTRA.get(k, ()))
        alts = [f"(?i:{re.escape(w)})s?" for w in sorted(ws, key=lambda w: (-len(w), w))]
        out[k] = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])")
    return out


def sink_shared_files():
    out = []
    for top in SINK_SHARED:
        full = os.path.join(LAB, top)
        if os.path.isfile(full):
            out.append(top)
            continue
        for d, ds, fs in os.walk(full):
            ds[:] = sorted(x for x in ds if x != "__pycache__" and os.path.relpath(os.path.join(d, x), LAB) != "lib/sinkkit")
            out += [os.path.relpath(os.path.join(d, f), LAB) for f in sorted(fs)]
    return out


def slug(heading):
    """A heading's anchor, as GitHub makes it."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def docs_missing():
    """Each kind's docs that aren't there: a page that isn't, or a section (#anchor) its page hasn't. Docs with no
    section (the kind's own README.md, or a whole page: manifests.py allows both) need only their page."""
    from sinkkit import manifests, registry
    out = []
    for k in registry.SINKS:
        docs = manifests.manifest(k)["docs"]
        page, _, anchor = docs.partition("#")
        try:
            heads = {slug(h) for h in re.findall(r"^#+ (.+)$", open(os.path.join(LAB, page)).read(), re.M)}
        except OSError:
            out.append(docs)
            continue
        if anchor and anchor not in heads:
            out.append(docs)
    return out


def sink_packages():
    print("the sinks' folders, their manifests, the generated files, and no kind named in shared code")
    from sinkkit import manifests, registry, vault
    folders = sorted(d for d in os.listdir(os.path.join(LAB, "sinks"))
                     if os.path.isdir(os.path.join(LAB, "sinks", d)) and d != "__pycache__")
    missing = [f"sinks/{k}/{f}" for k in registry.SINKS for f in ("manifest.json", "sink.py", "README.md")
               if not os.path.isfile(os.path.join(LAB, "sinks", k, f))]
    check("every registered kind is a folder with its manifest, its code and its page; every folder is registered",
          folders == sorted(registry.SINKS) and not missing and os.path.isfile(os.path.join(LAB, "sinks", "README.md")),
          (folders, registry.SINKS, missing))
    bad = vault.check() + manifests.conflicts()
    check("the manifests are what lib/sinkkit and the vault's build read (lib/sinkkit/vault.py check)", not bad, bad)
    shape = []
    for k in registry.SINKS:
        m = registry.module(k)
        if not callable(getattr(m, "check_url", None)):
            shape.append(f"{k}: no check_url")
        if hasattr(m, "request") == hasattr(m, "deliver"):
            shape.append(f"{k}: needs request (it POSTs) or deliver (a transport of its own), not both")
        if hasattr(m, "accepted") and not hasattr(m, "request"):
            shape.append(f"{k}: accepted goes with request")
    check("each kind's sink.py brings the interface: check_url, and request (accepted too, if it reads answers) or "
          "deliver", not shape, shape)
    unused = []
    for rel in ["lib/events.py"] + [f"lib/sinkkit/{f}" for f in sorted(os.listdir(os.path.join(LAB, "lib", "sinkkit")))
                                    if f.endswith(".py")] + [f"sinks/{k}/sink.py" for k in registry.SINKS]:
        tree = ast.parse(open(os.path.join(LAB, rel), encoding="utf-8").read())
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                unused += [f"{rel}: {a.name}" for a in n.names
                           if ((a.asname or a.name).split(".")[0] if isinstance(n, ast.Import) else a.asname or a.name)
                           not in used]
    check("the event stream's code (lib/events.py, lib/sinkkit, each kind's sink.py) imports nothing it doesn't use",
          not unused, unused)
    missing = docs_missing()
    check("each kind's docs are a page that's there, and the section they name is in it", not missing, missing)
    real, probe = registry.data, registry.SINKS[0]
    tried = {}
    for docs in (f"sinks/{probe}/README.md", "docs/events.md", "docs/events.md#no-such-section",
                 "docs/no-such-page.md"):
        changed = dict(real(probe), docs=docs)
        registry.data = lambda k, c=changed: c if k == probe else real(k)
        try:
            tried[docs] = (not manifests.check(probe), docs_missing())
        finally:
            registry.data = real
    check("...docs with no section (the kind's README.md, a whole page) pass both the manifest's check and this one; a "
          "section or a page that isn't there fails this one (codex review of item 5c, P3)",
          tried[f"sinks/{probe}/README.md"] == (True, []) and tried["docs/events.md"] == (True, [])
          and tried["docs/events.md#no-such-section"][1] == ["docs/events.md#no-such-section"]
          and tried["docs/no-such-page.md"][1] == ["docs/no-such-page.md"], tried)
    r = subprocess.run([PY, os.path.join(LAB, "scripts", "gen-sinks.py"), "--check"], capture_output=True, text=True,
                       timeout=60)
    check("the files made from the manifests are current: docs/sinks.md, `metacomputer help`'s line, CODEOWNERS "
          "(scripts/gen-sinks.py --check)", r.returncode == 0, r.stdout + r.stderr)
    words = sink_words()
    probes = [("def otlp_url(url):", ["otlp"]), ("syslog+tls://HOST:PORT", ["syslog"]), ("an ntfy URL names", ["ntfy"]),
              ("CloudEvents 1.0", ["webhook"]), ("def cloudevent(ev):", ["webhook"]), ("webhooks", ["webhook"]), ("OpenTelemetry logs", ["otlp"]),
              ("https://ntfy.sh/t", ["ntfy"]), ("SYSLOG_SEV", ["syslog"]), ("xwebhook ntfyx otlp2 webhookey", [])]
    wrong = [(t, [k for k, p in words.items() if p.search(t)]) for t, want in probes
             if [k for k, p in words.items() if p.search(t)] != want]
    check("the lint's patterns find the kinds' names where they are, and only there (tried on known lines first)",
          not wrong, wrong)
    hits, used = [], set()
    for rel in sink_shared_files():
        if rel in SINK_LINT_FILES:
            continue
        try:
            text = open(os.path.join(LAB, rel), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            named = [k for k, p in words.items() if p.search(line)]
            if not named:
                continue
            j = next((j for j, (f, piece, _w) in enumerate(SINK_LINT_ALLOW) if f == rel and piece in line), None)
            if j is not None:
                used.add(j)
                continue
            hits.append(f"{rel}:{i}: {','.join(named)}: {line.strip()[:120]}")
    check("no shared code names a kind of sink outside its folder (allowed: SINK_LINT_FILES and SINK_LINT_ALLOW, each "
          "for its reason)", not hits, hits[:12])
    stale = [SINK_LINT_ALLOW[j][:2] for j in range(len(SINK_LINT_ALLOW)) if j not in used] + sorted(
        f for f in SINK_LINT_FILES if not os.path.isfile(os.path.join(LAB, f))
        or not any(p.search(open(os.path.join(LAB, f)).read()) for p in words.values()))
    check("...and every allowance is still needed", not stale, stale)


# --- tests ----------------------------------------------------------------------------------
def main():
    sink_packages()
    hostile_manifests.sinks(check)
    sink_cases()
    certs = make_certs(os.path.join(TD, "certs"))
    web, otlp, ntfy = HTTPRx(), HTTPRx(), HTTPRx()
    sysrx = SyslogRx(certs[1], certs[2]) if certs else None
    token = "tk_ingest-only-" + "9" * 20
    try:
        print("sinks and configuration")
        r = cli("emit", "lifecycle", "started", "--message", "before any sink")
        check("emitting with no sinks works, creates no configuration and sends nothing",
              r.returncode == 0 and not os.path.exists(events.config_path()) and len(local_log()) == 1
              and "nothing was sent" in r.stdout, r.stdout + r.stderr)
        r = sink_add("web", "--kind", "webhook", "--url", web.url("/hook"))
        key = (re.search(r"^\s+([A-Za-z0-9+/=]{40,})$", r.stdout, re.M) or [None, None])[1]
        check("sink add webhook prints its signing key once", r.returncode == 0 and key, r.stdout + r.stderr)
        check("sink add otlp", sink_add("collector", "--kind", "otlp", "--url", otlp.url()).returncode == 0)
        r = sink_add("phone", "--kind", "ntfy", "--url", ntfy.url("/test-topic"), "--families", "needs_you",
                "--header", f"Authorization: Bearer {token}")
        check("sink add ntfy with a header", r.returncode == 0 and token not in r.stdout, r.stdout + r.stderr)
        if sysrx:
            r = sink_add("siem", "--kind", "syslog", "--url", f"syslog+tls://localhost:{sysrx.port}",
                    "--ca", certs[0])
            check("sink add syslog with its own CA", r.returncode == 0, r.stdout + r.stderr)
        else:
            print("  SKIP syslog (no openssl to make a test CA)")
        st = os.stat(events.config_path())
        check("events.json is 0600 and its folder 0700",
              oct(st.st_mode & 0o777) == "0o600" and oct(os.stat(events.events_dir()).st_mode & 0o777) == "0o700")
        lst, shw, js = cli("sink", "list"), cli("sink", "show", "phone"), cli("sink", "list", "--json")
        check("list and show never print header values or the ntfy topic",
              all(token not in x.stdout and "test-topic" not in x.stdout for x in (lst, shw, js))
              and "Authorization: (hidden)" in lst.stdout
              and next(s for s in json.loads(js.stdout) if s["name"] == "phone")["headers"] == ["Authorization"],
              lst.stdout + shw.stdout)
        r = cli("sink", "show", "web", "--key")
        check("sink show --key prints the signing key again", key and key in r.stdout, r.stdout)
        bad = [sink_add("x1", "--kind", "ntfy", "--url", "http://example.com/t"),
               sink_add("x2", "--kind", "webhook", "--url", "http://10.0.0.5/hook"),
               sink_add("x3", "--kind", "syslog", "--url", "https://example.com:6514"),
               sink_add("x4", "--kind", "webhook", "--url", "https://user:pw@example.com/h"),
               sink_add("web", "--kind", "webhook", "--url", web.url())]
        check("refused: plain http off this Mac, syslog without TLS, credentials in the URL, a duplicate name",
              all(b.returncode == 2 for b in bad), [b.stderr for b in bad])
        os.environ["MCOMP_TEST_TOKEN"] = "abcdefghijkl0123"
        r = sink_add("envhdr", "--kind", "webhook", "--url", web.url("/env"), "--header-env",
                "X-Api-Key=Key $MCOMP_TEST_TOKEN", "--families", "lifecycle", "--min-severity", "error")
        cfg = events.load_config()
        check("--header-env takes the value from the environment",
              r.returncode == 0 and cfg["sinks"]["envhdr"]["headers"] == [["X-Api-Key", "Key abcdefghijkl0123"]], r.stderr)
        sink_remove("envhdr")

        print("delivery to several sinks, in order, in each format")
        e1 = events.emit("task", "finished", "Task demo finished", data={"files": 2})
        e2 = events.emit("security", "blocked_connections", "The VM tried 1 blocked connection", data={"count": 1})
        e3 = events.emit("needs_you", "login", "Sign in to example.com needed", action="Sign in through the live view")
        seqs = [e1["seq"], e2["seq"], e3["seq"]]
        check("emit returns numbered events", seqs == sorted(seqs) and len(set(seqs)) == 3, seqs)
        got = [b["data"]["seq"] for b in web.bodies()]
        check("webhook got all three, in order", got == seqs, got)
        ol = [lr for b in otlp.bodies() for rl in b["resourceLogs"] for sl in rl["scopeLogs"] for lr in sl["logRecords"]]
        check("otlp got all three, in order", [int(a["value"]["intValue"]) for lr in ol for a in lr["attributes"]
                                               if a["key"] == "metacomputer.seq"] == seqs)
        nt = ntfy.bodies()
        check("ntfy (needs_you only) got just the third", len(nt) == 1 and "Sign in to example.com" in nt[0]["message"], nt)
        if sysrx:
            fr = wait_for(lambda: len(sysrx.frames) >= 3 and sysrx.frames)
            check("syslog got all three, in order", [parse_5424(f)["msg"]["seq"] for f in fr[:3]] == seqs, fr[:3])

        path, hdrs, body = web.reqs[2]
        ce = json.loads(body)
        check("CloudEvents 1.0 structured mode",
              ce["specversion"] == "1.0" and ce["type"] == "io.metavert.metacomputer.needs_you.login"
              and ce["source"] == f"metacomputer/{events.host_name()}" and ce["subject"] == "testvm"
              and ce["datacontenttype"] == "application/json" and ce["id"] == e3["id"]
              and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$", ce["time"])
              and hdrs["content-type"].startswith("application/cloudevents+json") and path == "/hook", ce)
        want = "sha256=" + hmac.new(base64.b64decode(key), body, hashlib.sha256).hexdigest()
        check("X-Metacomputer-Signature is HMAC-SHA256 of the exact body", hdrs.get("x-metacomputer-signature") == want)
        check("the event schema", set(ce["data"]) == {"id", "seq", "time", "family", "type", "severity", "origin", "vm", "project",
                                                      "message", "action", "data", "host"}
              and ce["data"]["action"] == "Sign in through the live view" and ce["data"]["severity"] == "warn", ce["data"])

        b = otlp.bodies()[0]
        rl = b["resourceLogs"][0]
        rattrs = {a["key"]: a["value"] for a in rl["resource"]["attributes"]}
        lr = rl["scopeLogs"][0]["logRecords"][0]
        attrs = {a["key"]: a["value"] for a in lr["attributes"]}
        check("OTLP: path /v1/logs, resource service.name/version and host.name",
              otlp.reqs[0][0] == "/v1/logs" and rattrs["service.name"] == {"stringValue": "metacomputer"}
              and "stringValue" in rattrs["service.version"] and "host.name" in rattrs, rattrs)
        check("OTLP: camelCase, int64 as strings, severity, body is the event JSON, attributes",
              isinstance(lr["timeUnixNano"], str) and lr["timeUnixNano"].isdigit() and lr["observedTimeUnixNano"].isdigit()
              and lr["severityNumber"] == 9 and lr["severityText"] == "INFO"
              and json.loads(lr["body"]["stringValue"])["seq"] == e1["seq"]
              and attrs["metacomputer.family"] == {"stringValue": "task"} and attrs["metacomputer.type"]["stringValue"] == "finished"
              and attrs["metacomputer.vm"]["stringValue"] == "testvm" and attrs["metacomputer.seq"] == {"intValue": str(e1["seq"])}
              and "metacomputer.project" in attrs, lr)

        if sysrx:
            f = parse_5424(sysrx.frames[2])
            check("syslog: RFC 5424 header, MSGID = type, structured data, MSG = event JSON",
                  f and f["pri"] == 1 * 8 + 4 and f["app"] == "metacomputer" and f["msgid"] == "login"
                  and f'seq="{e3["seq"]}"' in f["sd"] and 'family="needs_you"' in f["sd"]
                  and f["sd"].startswith("[metacomputer@32473 ") and f["msg"]["id"] == e3["id"], sysrx.frames[2])

        _, nh, _ = ntfy.reqs[0]
        n = nt[0]
        check("ntfy: JSON to the server root with the topic, high priority, the action, the header",
              ntfy.reqs[0][0] == "/" and n["topic"] == "test-topic" and n["priority"] == 5
              and "needs you: login" in n["title"] and "Sign in through the live view" in n["message"]
              and nh.get("authorization") == f"Bearer {token}", (n, nh))

        print("filters")
        refill()
        sec, err = HTTPRx(), HTTPRx()
        sink_add("sec", "--kind", "webhook", "--url", sec.url(), "--families", "security")
        sink_add("err", "--kind", "webhook", "--url", err.url(), "--min-severity", "error")
        events.emit("security", "chrome_input_refused", "refused")
        events.emit("lifecycle", "health", "VM gone", severity="error")
        events.emit("task", "finished", "done")
        check("--families security takes only security events",
              [b["data"]["type"] for b in sec.bodies()] == ["chrome_input_refused"], sec.bodies())
        check("--min-severity error takes only errors", [b["data"]["type"] for b in err.bodies()] == ["health"], err.bodies())
        n0 = len(local_log())
        check("actions events need a sink that asks for them", events.emit("actions", "call", "click called") is None
              and len(local_log()) == n0)
        act = HTTPRx()
        sink_add("act", "--kind", "webhook", "--url", act.url(), "--families", "actions")
        before, n0 = len(web.reqs), len(local_log())
        ev = events.emit("actions", "call", "click called", data={"tool": "click"})
        check("--families actions takes them; 'all' doesn't; the local log skips them",
              ev and len(act.reqs) == 1 and len(web.reqs) == before and len(local_log()) == n0, (len(act.reqs), len(web.reqs)))

        print("cleaning")
        refill()
        os.makedirs(ENV["MCOMP_SSH_DIR"], exist_ok=True)
        with open(os.path.join(ENV["MCOMP_SSH_DIR"], "vnc-pass"), "w") as fh:
            fh.write("Vnc7Pass\n")
        events._known["at"] = 0
        ev = events.emit("task", "finished",
                         "line\none\x1b[31m red\x1b[0m \u202eevil\x07 https://user:pw@x.example/a/b?token=abc#frag "
                         f"Bearer {token} Vnc7Pass " + "y" * 900,
                         action="open https://site.example/p?q=1",
                         data={"password": "pw", "text": "typed", "element_token": "t", "Authorization": "x",
                               "url": "https://h.example/x?session=1", "nested": {"a": {"b": {"c": {"d": {"e": 1}}}}},
                               "list": list(range(80)), "ok": True})
        m = ev["message"]
        check("control characters, ANSI sequences and bidi overrides are removed",
              not re.search("[\x00-\x1f\x7f\u202e]", m) and "[31m" not in m and m.startswith("line one red"), m[:80])
        check("URLs lose credentials, query and fragment", "https://x.example/a/b " in m and "token=abc" not in m
              and "user:pw" not in m and "frag" not in m and ev["action"] == "open https://site.example/p"
              and ev["data"]["url"] == "https://h.example/x", (m[:200], ev["action"], ev["data"].get("url")))
        check("a sink's header value and the live-view password never appear", token not in json.dumps(ev)
              and "Vnc7Pass" not in json.dumps(ev) and "[redacted]" in m)
        check("secret-looking keys are dropped", not {"password", "text", "element_token", "Authorization"} & set(ev["data"])
              and ev["data"]["ok"] is True, ev["data"])
        check("sizes are capped (message, lists, depth)", len(m) <= 500 and len(ev["data"]["list"]) == 50
              and ev["data"]["nested"]["a"]["b"]["c"] == {"d": None}, (len(m), ev["data"]["nested"]))
        check("nothing secret reached any receiver", all(token not in b.decode() and "Vnc7Pass" not in b.decode()
                                                         and "token=abc" not in b.decode() for _, _, b in web.reqs))

        print("spool (other producers)")
        refill()
        sp = events.spool_dir()
        events.private_dir(sp)
        base = len(web.reqs)
        for i, obj in enumerate([
                {"family": "needs_you", "type": "approval_waiting", "severity": "warn", "vm": "testvm",
                 "message": "The vault waits for your approval to fill a password", "action": "metacomputer secrets approve",
                 "data": {"secret": "bank", "site": "https://bank.example/login?x=1"}},
                {"family": "lifecycle", "type": "vault_unlocked", "message": "Vault unlocked for 30 min"}]):
            with open(os.path.join(sp, f"20261117T120000.00000{i}Z-ab{i}.json.tmp"), "w") as fh:
                json.dump(obj, fh)
            os.replace(os.path.join(sp, f"20261117T120000.00000{i}Z-ab{i}.json.tmp"),
                       os.path.join(sp, f"20261117T120000.00000{i}Z-ab{i}.json"))
        with open(os.path.join(sp, "20261117T120001.000000Z-bad.json"), "w") as fh:
            fh.write('{"family": "nope", "type": "x", "message": "m"}')
        with open(os.path.join(sp, "20261117T120002.000000Z-big.json"), "w") as fh:
            fh.write(json.dumps({"family": "task", "type": "x", "message": "m" * 70000}))
        r = cli("drain")
        types = [b["data"]["type"] for b in web.bodies()[base:]]
        check("drain takes valid spool files in name order, cleans them and delivers them",
              types == ["approval_waiting", "vault_unlocked"] and r.returncode == 0, (types, r.stdout, r.stderr))
        sv = web.bodies()[base]["data"]
        check("spool events are cleaned too (secret keys, URL queries) and numbered",
              "secret" not in sv["data"] and sv["data"]["site"] == "https://bank.example/login" and isinstance(sv["seq"], int), sv)
        check("every spool file is taken; invalid and oversized ones are quarantined",
              os.listdir(sp) == [".quarantine"] and len(os.listdir(os.path.join(sp, ".quarantine"))) == 2, os.listdir(sp))
        check("rejected files are counted", events.read_json(os.path.join(events.state_dir(), "spool.json"), {}).get("rejected") == 2)

        print("backlog and retry")
        refill()
        web.mode = "503"
        t0 = time.time()
        a1 = events.emit("task", "finished", "while the receiver is down 1")
        a2 = events.emit("task", "finished", "while the receiver is down 2")
        dt = time.time() - t0
        s = events.status_info()
        f = [x for x in s["failing"] if x["sink"] == "web"]
        check("a failing sink doesn't fail or hold up the emitter", a1 and a2 and dt < 4, dt)
        check("its backlog and last error are shown", f and f[0]["backlog"] == 2 and f[0]["error"] == "HTTP 503", s["failing"])
        r = cli("sink", "list")
        check("sink list shows the backlog and the error", re.search(r"web .*backlog 2 .*HTTP 503", r.stdout), r.stdout)
        web.mode = "ok"
        base = len(web.reqs)
        cli("drain")
        check("the watcher's drain respects the backoff", len(web.reqs) == base)
        cli("drain", "--now")
        got = [b["data"]["seq"] for b in web.bodies()[base:]]
        check("once it's back, the backlog arrives in order", got == [a1["seq"], a2["seq"]], got)
        check("and the backlog is empty", not [x for x in events.status_info()["failing"] if x["sink"] == "web"])
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(16)       # accepts connections, never answers
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()          # nothing listens here
        sink_add("hole", "--kind", "webhook", "--url", f"http://127.0.0.1:{hole.getsockname()[1]}/")
        sink_add("dead", "--kind", "otlp", "--url", f"http://127.0.0.1:{dead_port}")
        t0 = time.time()
        ev = events.emit("task", "finished", "with a sink that never answers")
        dt = time.time() - t0
        check("a sink that never answers costs at most about 3 s", ev and dt < 4.5, dt)
        hs, ds = stat_of("hole"), stat_of("dead")
        check("timeouts and refused connections are recorded", hs["last_error"] == "timed out"
              and ds["last_error"] == "connection refused", (hs["last_error"], ds["last_error"]))
        t0 = time.time()
        events.emit("task", "finished", "again, during the backoff")
        check("during its backoff a failing sink isn't tried at all", time.time() - t0 < 1.5, time.time() - t0)
        r = cli("sink", "test", "dead")
        check("sink test reports a failure", r.returncode == 1 and "connection refused" in r.stdout, r.stdout)
        r = cli("sink", "test", "web")
        check("sink test reports success", r.returncode == 0 and r.stdout.startswith("OK"), r.stdout + r.stderr)
        hole_gen = events.gen_of(events.load_config()["sinks"]["hole"])
        for n in ("hole", "dead"):
            sink_remove(n)
        hole.close()
        check("sink remove deletes the sink and its backlog", "hole" not in events.load_config()["sinks"]
              and not os.path.exists(events.sink_file("hole", hole_gen, "queue")))

        print("order with several processes at once")
        refill()
        base = len(web.reqs)
        procs = [subprocess.Popen([PY, EV, "emit", "task", "finished", "--message", f"parallel {i}", "--quiet"],
                                  env=os.environ.copy()) for i in range(8)]
        for p in procs:
            p.wait(timeout=60)
        cli("drain", "--now")
        got = [b["data"]["seq"] for b in web.bodies()[base:]]
        check("8 processes at once: every event once, in sequence order", len(got) == 8 and got == sorted(got)
              and len(set(got)) == 8, got)
        if sysrx:
            fr = [parse_5424(x)["msg"]["seq"] for x in sysrx.frames]
            check("the syslog sink also got everything in order", fr == sorted(fr) and len(fr) == len(set(fr)), fr[-12:])

        print("debounce")
        refill()
        d1 = events.emit("needs_you", "in_use", "busy", debounce="k1")
        d2 = events.emit("needs_you", "in_use", "busy", debounce="k1")
        d3 = events.emit("needs_you", "in_use", "busy", debounce="k2")
        time.sleep(0.6)
        d4 = events.emit("needs_you", "in_use", "busy", debounce="k1", debounce_s=0.5)
        check("an identical event within the window is dropped; another key or a later one isn't",
              d1 and d2 is None and d3 and d4, [bool(x) for x in (d1, d2, d3, d4)])

        print("notify")
        refill()
        base = len(ntfy.reqs)
        r = cli("notify", "Sign in to example.com to continue", "--why", "login", "--action", "Sign in through the live view")
        last = ntfy.bodies()[-1] if len(ntfy.reqs) > base else {}
        check("notify reaches the needs_you sink with the action", r.returncode == 0 and "phone delivered" in r.stdout
              and "needs you: login" in last.get("title", "") and "live view" in last.get("message", ""), (r.stdout, last))
        ll = local_log()[-1]
        check("notify's event: needs_you, type = why, warn, declared by Claude", ll["family"] == "needs_you"
              and ll["type"] == "login" and ll["severity"] == "warn" and ll["data"].get("declared_by") == "claude", ll)
        r = cli("notify", "x", "--why", "bogus")
        check("notify refuses an unknown --why", r.returncode == 2)

        print("the watcher's pieces")
        refill()
        ng = os.path.join(ENV["MCOMP_LOG_DIR"], "netgw-blocked.jsonl")
        with open(ng, "w") as fh:
            fh.write('{"t":"2026-11-17T10:00:00Z","event":"blocked","reason":"lab:private-lan","proto":"tcp","dst":"10.0.0.9:80"}\n')
        st = {}
        tail = events.NetgwTail(ng, st)
        check("the first run starts at the end of the log (no history)", tail.poll() == [])
        with open(ng, "a") as fh:
            for dst in ("10.0.0.1:9", "10.0.0.1:9", "[fd00::1]:443"):
                fh.write(json.dumps({"t": "2026-11-17T10:00:01Z", "event": "blocked", "reason": "lab:private-lan",
                                     "proto": "tcp", "dst": dst, "payload": "GET /secret"}) + "\n")
            fh.write('{"t":"partial')
        recs = tail.poll()
        check("new complete lines only", len(recs) == 3, recs)
        events.add_blocked(st, recs)
        bev = events.blocked_event(st, "testvm", now=time.time())
        ds = (bev or {}).get("data", {}).get("destinations", [])
        check("one batched security event per minute: counts by destination, port and reason, no payloads",
              bev and bev["data"]["count"] == 3 and ds[0] == {"host": "10.0.0.1", "port": 9, "proto": "tcp",
                                                               "reason": "lab:private-lan", "count": 2,
                                                               "first": "2026-11-17T10:00:01Z", "last": "2026-11-17T10:00:01Z"}
              and ds[1]["host"] == "fd00::1" and "payload" not in json.dumps(bev), bev)
        events.add_blocked(st, [{"dst": "10.0.0.2:22", "reason": "x"}])
        check("a second batch within the minute waits", events.blocked_event(st, "testvm", now=time.time()) is None)
        base = len(web.reqs)
        wst = events.watch_state_path("testvm")
        if os.path.exists(wst):
            os.remove(wst)
        r1 = cli("watch", "--once")
        with open(ng, "a") as fh:
            fh.write('cut"}\n')
            fh.write(json.dumps({"t": "2026-11-17T10:01:00Z", "event": "blocked", "reason": "default:host-address",
                                 "proto": "udp", "dst": "192.0.2.10:53"}) + "\n")
        r2 = cli("watch", "--once")
        got = [b["data"] for b in web.bodies()[base:] if b["data"]["type"] == "blocked_connections"]
        check("watch --once: a new blocked connection becomes a security event in one cycle",
              r1.returncode == 0 and r2.returncode == 0 and len(got) == 1 and got[0]["data"]["destinations"][0]["host"] == "192.0.2.10",
              (r1.stderr, r2.stderr, got))

        # Q4: the running watcher looks at the gateway's log between its 30-second cycles, so the first blocked
        # connection after a quiet spell reaches the sinks within seconds; the follow-ups wait for the batch.
        refill()
        if os.path.exists(wst):
            os.remove(wst)
        wlog = open(os.path.join(TD, "q4-watcher.log"), "w")
        wp = subprocess.Popen([PY, EV, "watch"], stdout=wlog, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            cycled = wait_for(lambda: os.path.exists(wst), 20)
            time.sleep(0.5)
            base = len(web.reqs)
            t_block = time.time()
            with open(ng, "a") as fh:
                fh.write(json.dumps({"t": "2026-11-17T10:05:00Z", "event": "blocked", "reason": "lab:private-lan",
                                     "proto": "tcp", "dst": "10.9.9.9:22"}) + "\n")

            def blocked_got():
                return [b["data"] for b in web.bodies()[base:] if b["data"]["type"] == "blocked_connections"]
            first_got = wait_for(blocked_got, 12)
            took = time.time() - t_block
            check("Q4 the running watcher sends the first blocked connection after a quiet spell within seconds "
                  "(not at its next 30-second cycle), marked first",
                  cycled and first_got and took < 8 and first_got[0]["data"].get("first") is True
                  and first_got[0]["data"]["destinations"][0]["host"] == "10.9.9.9", (cycled, round(took, 1), first_got))
            with open(ng, "a") as fh:
                for k in range(5):
                    fh.write(json.dumps({"t": "2026-11-17T10:05:01Z", "event": "blocked", "reason": "lab:private-lan",
                                         "proto": "tcp", "dst": f"10.9.9.{k + 10}:445"}) + "\n")
            time.sleep(6)
            wst_now = events.read_json(wst, {})
            pend = sum(int(e.get("count") or 0) for e in wst_now.get("netgw_pending") or [])
            check("Q4 the burst that follows is batched: nothing more is sent at once, the 5 wait in the watcher's state",
                  len(blocked_got()) == 1 and pend == 5, (len(blocked_got()), pend))
        finally:
            wp.terminate()
            try:
                wp.wait(15)
            except subprocess.TimeoutExpired:
                wp.kill()
            wlog.close()

        # Q5: ...also while the watcher's own cycle is busy: here a health probe that takes 20 s (a stand-in for the real
        # one, which may take minutes) holds the cycle, and the gateway's log is still watched on its own thread.
        refill()
        if os.path.exists(wst):
            os.remove(wst)
        slow = os.path.join(TD, "q5-slow-watcher.py")
        with open(slow, "w") as fh:
            fh.write("import sys, time\n"
                     f"sys.path.insert(0, {os.path.join(LAB, 'lib')!r})\n"
                     "import events\n"
                     "def probe():\n"
                     "    time.sleep(20)\n"
                     "    return {'vm': 'running', 'gateway': True, 'ssh': True, 'recording': {'expected': False},\n"
                     "            'guest': {'ready': True, 'driver_doctor_rc': 0, 'disk_free_gb': 20}}\n"
                     "events.probe_health = probe\n"
                     "sys.exit(events.watch(health=True))\n")
        wlog = open(os.path.join(TD, "q5-watcher.log"), "w")
        wp = subprocess.Popen([PY, slow], stdout=wlog, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            time.sleep(3)                        # the first cycle is in its 20 s probe now
            base = len(web.reqs)
            t_block = time.time()
            with open(ng, "a") as fh:
                fh.write(json.dumps({"t": "2026-11-17T10:06:00Z", "event": "blocked", "reason": "lab:private-lan",
                                     "proto": "tcp", "dst": "10.8.8.8:22"}) + "\n")

            def blocked_got5():
                return [b["data"] for b in web.bodies()[base:] if b["data"]["type"] == "blocked_connections"]
            got5 = wait_for(blocked_got5, 15)
            took = time.time() - t_block
            check("Q5 while the watcher's cycle is busy (a 20 s health probe), the first blocked connection still reaches "
                  "the sinks within seconds", got5 and took < 8 and got5[0]["data"].get("first") is True
                  and wp.poll() is None, (round(took, 1), got5, wp.poll()))
        finally:
            wp.terminate()
            try:
                wp.wait(30)
            except subprocess.TimeoutExpired:
                wp.kill()
            wlog.close()

        # Q6: ...and when the cycle itself reads that first blocked connection (not the poller), it goes out at once too,
        # not after the cycle's health probe (12 s here) and deliveries.
        refill()
        st6 = {}
        tail6 = events.NetgwTail(ng, st6)
        with open(ng, "a") as fh:
            fh.write(json.dumps({"t": "2026-11-17T10:07:00Z", "event": "blocked", "reason": "lab:private-lan",
                                 "proto": "tcp", "dst": "10.7.7.7:22"}) + "\n")
        real_probe = events.probe_health

        def slow_probe():
            time.sleep(12)
            return {"vm": "running", "gateway": True, "ssh": True, "recording": {"expected": False},
                    "guest": {"ready": True, "driver_doctor_rc": 0, "disk_free_gb": 20}}
        events.probe_health = slow_probe
        base = len(web.reqs)
        try:
            cyc = threading.Thread(target=events.watch_cycle, args=(st6, tail6, "testvm"), kwargs={"health": True})
            t6 = time.time()
            cyc.start()

            def got6():
                return [b["data"] for b in web.bodies()[base:] if b["data"]["type"] == "blocked_connections"]
            g6 = wait_for(got6, 8)
            took6 = time.time() - t6
            check("Q6 a first blocked connection the watcher's own cycle reads goes out at once, not after the cycle's 12 s "
                  "health probe", g6 and took6 < 6 and cyc.is_alive() and g6[0]["data"].get("first") is True
                  and g6[0]["data"]["destinations"][0]["host"] == "10.7.7.7", (round(took6, 1), g6, cyc.is_alive()))
            cyc.join(30)
        finally:
            events.probe_health = real_probe
        hst = {}
        good = {"vm": "running", "gateway": True, "ssh": True, "guest": {"ready": True, "driver_doctor_rc": 0, "disk_free_gb": 20},
                "recording": {"expected": False}}
        sick = {"vm": "running", "gateway": True, "ssh": True, "guest": {"ready": True, "driver_doctor_rc": 2, "disk_free_gb": 1.5},
                "recording": {"expected": True, "name": "demo", "running": False}}
        e_ok = events.health_events(hst, good, "testvm")
        e_sick = events.health_events(hst, sick, "testvm")
        e_same = events.health_events(hst, sick, "testvm")
        e_back = events.health_events(hst, good, "testvm")
        check("health: one event per condition change (doctor, disk, recording), then 'recovered'",
              e_ok == [] and sorted(e["data"]["condition"] for e in e_sick) == ["disk_low", "doctor", "recording_failed"]
              and e_same == [] and sorted(e["data"]["condition"] for e in e_back) == ["disk_low", "doctor", "recording_failed"]
              and all(e["severity"] == "info" for e in e_back)
              and {e["data"]["condition"]: e["severity"] for e in e_sick}["recording_failed"] == "error", (e_sick, e_back))
        e_off = events.health_events({}, {"vm": "stopped"}, "testvm")
        check("health: a VM that stopped by itself is an error", [(e["data"]["condition"], e["severity"]) for e in e_off]
              == [("vm_off", "error")], e_off)

        print("the bridge's hooks (no VM): in the hub, for every client")
        refill()
        import mcp_hub
        b = mcp_hub.Hub(env=dict(os.environ), inject_sessions=False, local=True)
        sent = []
        c = mcp_hub.Client(b, None, os.getpid())
        c.send = sent.append
        c.hello, c.kind, c.cwd = True, "claude-code", os.getcwd()
        b.busy_others = lambda client: [{"kind": "session", "claude_pid": 4242, "cwd": "/example/other-project",
                                         "idle_s": 30, "last_tool": "click", "active": True}]
        b.connect = lambda: None
        n0 = len(local_log())
        b.on_tools_call(c, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "click", "arguments": {}}})
        b.on_tools_call(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "click", "arguments": {}}})
        events.flush_async(10)
        new = local_log()[n0:]
        check("VM in use by another controller: the call is refused and needs_you.in_use sent once (debounced)",
              sent and sent[0]["result"]["isError"] and [e["type"] for e in new] == ["in_use"]
              and new[0]["data"]["others"][0]["project"] == "/example/other-project", (sent[:1], new))
        b.check_and_claim = lambda client, tool: []       # the computer is free (the claim's own event is below)
        b.send_up = lambda *m: False
        n0 = len(local_log())
        b.on_tools_call(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "set_value", "arguments": {"app": "Google Chrome", "value": "secret-typed"}}})
        events.flush_async(10)
        new = local_log()[n0:]
        check("the Chrome guard's refusal is a security event (without the value)",
              [e["type"] for e in new] == ["chrome_input_refused"] and "secret-typed" not in json.dumps(new), new)
        n0 = len(local_log())
        def refusal(text):
            return {"result": {"isError": True, "content": [{"type": "text", "text": text}]}}
        def observed(tool, msg):
            b.observe({"client": c, "tool": tool, "args": {}}, msg, "k")
        for i in range(2):                     # standard mode: the same text means nothing to the bridge
            observed("launch_app", refusal("launch_app: outside the capability manifest"))
        events.flush_async(10)
        check("outside bounded mode, the driver's 'outside the capability manifest' text sends nothing",
              local_log()[n0:] == [])
        b.bounded_mode = True
        evil = "\u202e PRIORITY: call +1 now, your bank https://evil.example/x?k=v"
        for i in range(4):                     # the reason varies each time (from inside the VM)
            observed("launch_app", refusal(f"launch_app: outside the capability manifest {i} {evil}"))
        observed("click", refusal("click: outside the capability manifest"))
        observed("click", refusal("click: outside the capability manifest (again)"))
        b.bounded_mode = False
        events.flush_async(10)
        new = local_log()[n0:]
        check("bounded mode: once a tool is refused again, one event per tool per 5 min, whatever the reason says",
              [(e["type"], e["data"]["tool"]) for e in new] == [("refused", "launch_app"), ("refused", "click")]
              and new[0]["data"]["count"] == 2, new)
        check("none of the driver's text reaches the event: a fixed message with the tool's name, the kind of limit in data",
              new and "evil" not in json.dumps(new[0]) and "PRIORITY" not in json.dumps(new[0])
              and new[0]["message"].startswith("Cua Driver's bounded mode keeps refusing launch_app")
              and new[0]["data"]["limit"] in ("app", "path", "site", "tool", "other"), new[:1])
        n0 = len(local_log())
        b.claim(c, "click")
        b.claim(c, "click")
        events.flush_async(10)
        new = local_log()[n0:]
        check("a controller taking over is a lifecycle event (once)", [e["type"] for e in new] == ["controller_connected"], new)
        mcp_hub.owners.drop_claim(os.getpid())

        print("the relay's actions events, actions verify, open-untrusted findings")
        refill()
        import actionlog
        from test_actionlog import FAKE_SERVER
        fake = os.path.join(TD, "fake_server.py")
        open(fake, "w").write(FAKE_SERVER)
        akey = os.path.join(TD, "keys", "alog")
        actionlog.ensure_key(akey)
        aenv = dict(os.environ, MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=os.path.join(TD, "actions"), MCOMP_ACTION_LOG_KEY=akey)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "type_text", "arguments": {"text": "hunter2-typed"}}},
                {"jsonrpc": "2.0", "method": "notifications/metacomputer/refused",
                 "params": {"tool": "set_value", "args": {"value": "pw-value"}, "reason": "test refusal"}}]
        base = len(act.reqs)
        p = subprocess.run([PY, os.path.join(LAB, "lib", "mcp_relay.py"), PY, fake],
                           input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, env=aenv, timeout=60)
        cli("drain", "--now")
        adata = [b["data"] for b in act.bodies()[base:]]
        kinds = [d["type"] for d in adata]
        check("with a sink for actions, each signed record becomes an event, in order",
              p.returncode == 0 and kinds[:1] == ["start"] and "call" in kinds and "refused" in kinds and "result" in kinds
              and kinds[-1] == "checkpoint" and [d["seq"] for d in adata] == sorted(d["seq"] for d in adata), kinds)
        blob = json.dumps(adata)
        check("actions events carry tool names and record hashes, never arguments or typed text",
              "hunter2-typed" not in blob and "pw-value" not in blob and all(len(d["data"].get("hash") or "") == 64 for d in adata)
              and any(d["data"].get("tool") == "type_text" for d in adata), blob[:300])
        alog = actionlog.files(os.path.join(TD, "actions"))[-1]
        lines = open(alog).read().splitlines()
        rec = json.loads(lines[1])
        rec["tool"] = "changed"
        lines[1] = json.dumps(rec)
        open(alog, "w").write("\n".join(lines) + "\n")
        n0 = len(local_log())
        v = subprocess.run([PY, os.path.join(LAB, "lib", "actionlog.py"), "verify", alog, "--key", akey + ".pub"],
                           capture_output=True, text=True, env=aenv, timeout=60)
        new = local_log()[n0:]
        check("a failed actions verify is a security error event",
              v.returncode == 1 and [(e["type"], e["severity"]) for e in new] == [("actions_verify_failed", "error")], (v.stdout, new))
        from browser_tools import untrusted_findings
        f = untrusted_findings("https://start.example/p?ref=1",
                               [{"status": "302", "location": "https://evil.example/login?session=abc"}, {"status": "200"}],
                               [{"tabs": [("https://evil.example/login?session=abc", "Sign in")], "password_fields": 1}],
                               "https://evil.example/login?session=abc", 1, ["https://evil.example/login?session=abc"],
                               ["invoice.pdf.exe"])
        check("open-untrusted findings: password, download, another site, by origin only",
              f and f["final_origin"] == "https://evil.example" and f["redirected_off_site"] and f["downloads"] == 1
              and "asked for a password" in f["summary"] and "session=abc" not in json.dumps(f)
              and "ref=1" not in json.dumps(f), f)
        check("open-untrusted with nothing notable sends nothing",
              untrusted_findings("https://a.example/", [], [], "https://www.a.example/x", 0, [], []) is None)

        print("audit fixes")
        refill()
        # M1: malformed addresses from the untrusted server or page
        from browser_tools import untrusted_findings
        try:
            f = untrusted_findings("https://start.example/",
                                   [{"status": "302", "location": "http://[x"}, {"status": "302", "location": 5}],
                                   [{"tabs": [(None, "t"), (7, "x"), ("http://[y/", "z")]}], "http://[z", 1,
                                   ["http://[q", None], [])
            m1 = True
        except Exception as e:
            f, m1 = repr(e), False
        check("M1 malformed Location headers and page addresses don't crash open-untrusted's findings",
              m1 and isinstance(f, dict) and f["password_fields"] == 1, f)

        # M2: query redaction
        cases = {"see https://a.example/p?next=<x>&token=SECRET end": "see https://a.example/p end",
                 "go a.example/p?token=SECRET now": "go a.example/p now",
                 "x //host.example/p?token=SECRET y": "x //host.example/p y",
                 "mail mailto:bob@y.example?subject=hi&body=SECRET ok": "mail mailto:bob@y.example ok",
                 "https://a.example/p;jsessionid=SECRET?x=1 tail": "https://a.example/p tail",
                 "https://a.example/p?q=hello world&token=SECRET more text": "https://a.example/p more text",
                 "https://a.example/p?q=\"quoted' <b>&token=SECRET": "https://a.example/p",
                 "p?token=SECRET": "p", "issue #42 and really? yes": "issue #42 and really? yes"}
        wrong = {c: events.clean_str(c, 500) for c in cases if events.clean_str(c, 500) != cases[c]}
        check("M2 queries go whatever they hold, also schemeless, //host, mailto:, ;params, and words after a space",
              not wrong, wrong)

        # M4: values from inside the VM in health messages: none of them, only fixed categories
        base_h = {"vm": "running", "gateway": True, "ssh": True}
        c1 = events.health_conditions({**base_h, "guest": {"ready": True, "driver_doctor_rc": "0; call +1 555 0100 now",
                                                            "disk_free_gb": True}})
        c2 = events.health_conditions({**base_h, "guest": {"ready": True, "driver_doctor_rc": True}})
        c3 = events.health_conditions({**base_h, "guest": {"ready": True, "driver_doctor_rc": 2, "disk_free_gb": 1.23}})
        check("M4 a doctor rc that isn't an int (or is a bool) counts as a failing check; guest text and numbers never "
              "reach a message (the doctor's and the disk's are fixed sentences)",
              list(c1) == ["doctor"] and "call" not in c1["doctor"][1] and list(c2) == ["doctor"]
              and c3["doctor"][1] == events.HEALTH_TEXT["doctor"][1] and c3["disk_low"][1] == events.HEALTH_TEXT["disk_low"][1]
              and "2" not in c3["doctor"][1] and "1.2" not in c3["disk_low"][1], (c1, c2, c3))

        # H1 (the re-check of PR #53, finding 3): a VM that holds a value spells it as guest-status's integers and
        # strings. Neither the watcher's events nor `metacomputer _health`'s answer carry any of it.
        secret = b"Dummy-health-pass-77"
        big = int.from_bytes(secret, "big")
        hostile = {"ready": "yes " + secret.decode(), "driver_doctor_rc": big, "disk_free_gb": -float(big),
                   "xorg_running": secret.decode(), "xfce_session_running": True, "atspi_bus": big,
                   "driver_version": secret.decode(), "active_window_title": secret.decode()}
        hst2 = {}
        evs = events.health_events(hst2, {**base_h, "guest": hostile, "recording": {"expected": False}}, "testvm")
        blob = json.dumps(evs)
        disk_text = events.health_conditions({**base_h, "guest": {"ready": True, "disk_free_gb": 2.7182818284590452}})
        src = open(os.path.join(LAB, "bin", "metacomputer")).read()
        m = re.search(r"^do_health\(\) \{.*?<<'EOF'\n(.*?)\nEOF\n", src, re.S | re.M)
        out = {}
        if m:
            pr = subprocess.run([sys.executable, "-c", m.group(1), "running", "true", "true", json.dumps(hostile), "", "", "",
                                 ""], capture_output=True, text=True, timeout=30)
            try:
                out = json.loads(pr.stdout or "{}")
            except ValueError:
                out = {"unparsed": pr.stdout[-300:], "err": pr.stderr[-300:]}
        dumped = json.dumps(out)
        check("H1 a doctor 'exit code' of int.from_bytes(a value), and guest-status strings spelling it: the health "
              "events carry neither the number nor the text (a fixed sentence and categories), and `_health` itself "
              "answers only categories (yes/no, ok/failed/unknown, ok/low/unknown)",
              evs and str(big) not in blob and secret.decode() not in blob
              and sorted(e["data"]["condition"] for e in evs) == ["doctor", "not_ready"]
              and all(set(e["data"]) == {"condition", "state"} for e in evs)
              and "2.7" not in json.dumps(disk_text)
              and m is not None and str(big) not in dumped and secret.decode() not in dumped
              and out.get("guest") == {"ready": False, "xorg_running": False, "xfce_session_running": True,
                                       "atspi_bus": False, "driver_doctor": "failed", "disk": "unknown"},
              (evs, out))
        gh = getattr(events, "guest_health", lambda g: None)({"ready": True, "driver_doctor": "failed; " + secret.decode(),
                                                              "disk": big})
        check("H1 categories that come back out of range (an older or tampered `_health`) are read as unknown",
              gh == {"ready": True, "doctor": "unknown", "disk": "unknown"}, gh)

        # M5: rate limit per family, with a summary
        refill()
        base = len(ntfy.reqs)
        for i in range(12):
            events.emit("needs_you", "other", f"rate test {i}")
        logged = [e for e in local_log() if str(e.get("message", "")).startswith("rate test")]
        check("M5 at most 10 'needs you' events per 10 min reach a sink; all 12 are in the local log, 2 marked",
              len(ntfy.reqs) - base == 10 and len(logged) == 12 and sum(1 for e in logged if e.get("rate_limited")) == 2,
              (len(ntfy.reqs) - base, len(logged)))
        age_window("ratelimit.json", "needs_you", 601)
        age_window("phonelimit.json", "phone", 601)
        events.emit("needs_you", "other", "after the window")
        tail2 = ntfy.bodies()[base + 10:]
        check("M5 then one summary of what was held back, before the next event",
              [b["title"] for b in tail2] == ["testvm - needs you: suppressed", "testvm - needs you: other"]
              and "2 more" in tail2[0]["message"], tail2)
        refill()
        for i in range(11):
            events.emit("security", "chrome_input_refused", f"burst {i}", deliver_now=False)
        age_window("ratelimit.json", "security", 601)
        base = len(web.reqs)
        cli("drain", "--now")
        types = [b["data"]["type"] for b in web.bodies()[base:]]
        check("M5 the watcher's cycle sends a waiting summary even when nothing new comes", types[-1:] == ["suppressed"], types)
        st, t0 = {}, 1_000_000.0
        same = [{"dst": "10.0.0.1:9", "reason": "r", "proto": "tcp", "t": "x"}]
        events.add_blocked(st, same)
        b1 = events.blocked_event(st, "testvm", now=t0)
        events.add_blocked(st, same)
        b2 = events.blocked_event(st, "testvm", now=t0 + 61)
        b3 = events.blocked_event(st, "testvm", now=t0 + 121)
        events.add_blocked(st, same)
        b4 = events.blocked_event(st, "testvm", now=t0 + 121 + 121)
        b5 = events.blocked_event(st, "testvm", now=t0 + 121 + 241)
        events.add_blocked(st, [{"dst": "10.0.0.2:22", "reason": "r", "proto": "tcp"}])
        b6 = events.blocked_event(st, "testvm", now=t0 + 362 + 61)
        check("M5 the same blocked destinations again: the wait doubles (1, 2, 4 min ...); a new set starts over",
              b1 and b2 is None and b3 and b3["data"]["count"] == 1 and b3["data"]["same_as_before"] and b4 is None
              and b5 and b6 and not b6["data"]["same_as_before"], [bool(x) for x in (b1, b2, b3, b4, b5, b6)])

        # Q1-Q3: the first blocked connection after a quiet spell goes out at once; follow-ups are batched; repeats
        # and floods are still limited.
        st, t0 = {}, 2_000_000.0
        a = [{"dst": "10.0.0.7:22", "reason": "r", "proto": "tcp", "t": "x"}]
        events.add_blocked(st, a)
        q1 = events.blocked_event(st, "testvm", now=t0)
        events.add_blocked(st, a * 5)
        q2 = events.blocked_event(st, "testvm", now=t0 + 2)
        q3 = events.blocked_event(st, "testvm", now=t0 + 100)
        q4 = events.blocked_event(st, "testvm", now=t0 + 125)
        events.add_blocked(st, a)
        q5 = events.blocked_event(st, "testvm", now=t0 + 135)      # 133 s after the last one seen: not a quiet spell
        q5b = events.blocked_event(st, "testvm", now=t0 + 370)     # the doubled wait (4 min) is over: batched
        st_old = json.loads(json.dumps(st))
        events.add_blocked(st, a)
        q6 = events.blocked_event(st, "testvm", now=t0 + 440)      # 305 s after the last one seen: a quiet spell
        check("Q1 the first blocked connection goes at once (first: true); the burst after it is batched (one event "
              "after the wait, first: false); after 5 quiet minutes the same destinations go at once again, though "
              "the doubled wait (8 min) isn't over",
              q1 and q1["data"]["first"] is True and q1["data"]["count"] == 1 and q2 is None and q3 is None
              and q4 and q4["data"]["first"] is False and q4["data"]["count"] == 5 and q5 is None
              and q5b and q5b["data"]["first"] is False and q5b["data"]["count"] == 1
              and q6 and q6["data"]["first"] is True and q6["data"]["count"] == 1 and q6["data"]["same_as_before"],
              [x and (x["data"]["first"], x["data"]["count"]) for x in (q1, q2, q3, q4, q5, q5b, q6)])
        st_cmp = json.loads(json.dumps(st_old))
        events.add_blocked(st_cmp, a)
        st_cmp["netgw_seen"] = t0 + 439
        check("Q1 (without the quiet spell, the same moment waits for the doubled wait, as every report did before)",
              events.blocked_event(st_cmp, "testvm", now=t0 + 440) is None)
        st, t0, sent = {}, 3_000_000.0, []
        for k in range(300):                                        # a flood: a new destination every 2 s for 10 min
            events.add_blocked(st, [{"dst": f"10.1.{k // 250}.{k % 250}:80", "reason": "r", "proto": "tcp"}])
            ev = events.blocked_event(st, "testvm", now=t0 + 2 * k)
            if ev:
                sent.append((2 * k, ev["data"]["first"]))
        gaps = [b[0] - a_[0] for a_, b in zip(sent, sent[1:])]
        check("Q2 a flood of blocked connections: one first event at once, then at most one batched event a minute",
              sent and sent[0] == (0, True) and all(not f for _, f in sent[1:]) and all(g >= 60 for g in gaps)
              and len(sent) <= 11, sent)
        st, t0, firsts = {}, 4_000_000.0, []
        for k in range(8):                                          # a quiet spell, then one blocked connection, 8 times
            events.add_blocked(st, a)
            ev = events.blocked_event(st, "testvm", now=t0 + 301 * k)
            firsts.append(ev["data"]["first"] if ev else None)
        events.add_blocked(st, a)
        later = events.blocked_event(st, "testvm", now=t0 + 301 * 7 + 3600)
        check("Q3 repeats after quiet spells: at most 6 go at once in any hour; the rest wait like follow-ups (the same "
              "destinations: the doubled wait), and an hour on they go at once again",
              firsts == [True] * 6 + [None, None] and later and later["data"]["first"] is True, (firsts, later and later["data"]))
        refill()
        for k in range(12):
            st_k = {}
            events.add_blocked(st_k, [{"dst": f"10.2.0.{k}:80", "reason": "r", "proto": "tcp"}])
            events.emit_dict(events.blocked_event(st_k, "testvm"), origin="watcher")
        sec = [e for e in local_log() if e.get("type") == "blocked_connections"][-12:]
        check("Q3 and the family limit still holds over first events from separate watchers: 10 in 10 minutes reach the "
              "sinks, the rest only the local log", sum(1 for e in sec if e.get("rate_limited")) == 2, [e.get("rate_limited") for e in sec])
        events.flush_all(events.config_safe(), 10)                  # what was queued goes now, not into later checks
        refill()
        hst = {}
        sick = {**base_h, "guest": {"ready": True, "driver_doctor_rc": 2}}
        good = {**base_h, "guest": {"ready": True, "driver_doctor_rc": 0}}
        seq = [events.health_events(hst, sick, "testvm", now=t0), events.health_events(hst, good, "testvm", now=t0 + 300),
               events.health_events(hst, sick, "testvm", now=t0 + 600), events.health_events(hst, good, "testvm", now=t0 + 900),
               events.health_events(hst, sick, "testvm", now=t0 + 2000)]
        check("M5 a flapping health condition: announced, recovered, then quiet for 30 min, then announced again",
              [len(x) for x in seq] == [1, 1, 0, 0, 1], [len(x) for x in seq])
        refill()
        r1 = cli("notify", "Same text twice", "--why", "login")
        r2 = cli("notify", "Same text twice", "--why", "login")
        check("M5 notify: the same message within 5 min isn't sent again", "notified" in r1.stdout
              and "Not sent again" in r2.stdout, r1.stdout + r2.stdout)
        r1, r2 = cli("emit", "task", "finished", "--message", "dup"), cli("emit", "task", "finished", "--message", "dup")
        r3 = cli("emit", "task", "finished", "--message", "dup", "--debounce", "0")
        check("M5 events emit: the same event within 60 s is dropped (--debounce 0 turns that off)",
              r1.stdout.startswith("event ") and "not sent again" in r2.stdout and r3.stdout.startswith("event "),
              r1.stdout + r2.stdout + r3.stdout)

        # M6: sink changes need a terminal and are announced
        r = cli("sink", "add", "evil", "--kind", "webhook", "--url", "https://exfil.example/h")
        r2 = cli("sink", "remove", "web")
        check("M6 without a terminal, sink add and remove are refused", r.returncode == 2 and r2.returncode == 2
              and "your own terminal" in r.stderr and "evil" not in events.load_config()["sinks"]
              and "web" in events.load_config()["sinks"], r.stderr + r2.stderr)
        r = tty_cli("sink", "add", "cancelme", "--kind", "webhook", "--url", web.url("/c"), answer="n\n")
        check("M6 at a terminal it asks; anything but yes cancels", r.returncode == 2 and "cancelled" in r.stderr
              and "cancelme" not in events.load_config()["sinks"], r.stderr)
        rx2 = HTTPRx()
        bw, bn, bl = len(web.changes), len(ntfy.changes), len(local_log())
        r = tty_cli("sink", "add", "newone", "--kind", "webhook", "--url", rx2.url("/secret-path-segment"), answer="y\n")
        ch = [json.loads(x)["data"] for _, _, x in web.changes[bw:]]
        check("M6 typed yes adds it; the sinks that existed are told (masked URL, whatever their families); not the new one",
              r.returncode == 0 and ch and ch[-1]["type"] == "sink_added" and ch[-1]["data"]["sink"] == "newone"
              and "secret-path-segment" not in json.dumps(ch[-1]) and "***" in ch[-1]["data"]["url"]
              and len(ntfy.changes) > bn and not rx2.reqs and not rx2.changes
              and any(e["type"] == "sink_added" for e in local_log()[bl:]), (r.stderr, ch[-1:]))
        bw = len(web.changes)
        r = sink_remove("newone")
        rem = [json.loads(x)["data"]["type"] for _, _, x in rx2.changes]
        check("M6 removing it tells every sink that existed, the removed one included", r.returncode == 0
              and rem == ["sink_removed"] and len(web.changes) > bw and "newone" not in events.load_config()["sinks"],
              (r.stderr, rem))
        rx2.close()

        # L2, L3: more secret-looking keys; signing keys, topics and remembered values redacted
        ev = events.emit("task", "finished", "keys", data={k: "x" for k in (
            "passcode", "pwd", "key", "hmac_key", "access_key", "sig", "signature", "jwt", "sessionid", "bearer", "otp_code",
            "verification_code", "key_moments", "design", "assign")})
        check("L2 passcode, pwd, *key, sig, jwt, session, bearer, *_code are dropped; key_moments, design stay",
              set(ev["data"]) == {"key_moments", "design", "assign"}, ev["data"])
        hk = events.load_config()["sinks"]["web"]["hmac_key"]
        events._known["at"] = 0
        ev = events.emit("task", "finished", f"leak {hk} test-topic end")
        check("L3 a webhook signing key and an ntfy topic are redacted",
              hk not in ev["message"] and "test-topic" not in ev["message"]
              and ev["message"].count("[redacted]") == 2, ev["message"])
        # A sink another process adds: its header value is hidden from this process's very next event. The list of values
        # to hide was kept 30 s whatever events.json did, so a long-running process (the hub, a bridge) could send a
        # new header's value in that time; it's read again now whenever events.json's stat changes.
        events.known_secrets()                      # the list read just now: well within its 30 s
        fresh_val, frx = "dummy-fresh-header-value-58", HTTPRx()
        r = sink_add("fresh", "--kind", "webhook", "--url", frx.url("/fresh"), "--families", "task",
                     "--header", f"X-Fresh: {fresh_val}")
        n_web = len(web.reqs)
        ev = events.emit("task", "finished", f"leak {fresh_val} end", data={"note": f"also {fresh_val}"})
        sent_ = [b for _, _, b in web.reqs[n_web:]] + [b for _, _, b in frx.reqs]
        check("a header another process adds to events.json is redacted from this process's very next event (its "
              "message, data, local log and every sink), not 30 s later",
              r.returncode == 0 and ev and fresh_val not in json.dumps(ev) and "[redacted]" in ev["message"]
              and fresh_val not in json.dumps(local_log()[-1]) and sent_ and not any(fresh_val.encode() in b for b in sent_),
              (r.stderr[-200:], ev and ev["message"], len(sent_)))
        sink_remove("fresh")
        frx.close()
        r = cli("_remember-redaction", "vnc")
        check("R7 no stored tag of the live-view password: the command is gone and nothing like it is kept",
              r.returncode == 2 and not os.path.exists(os.path.join(events.state_dir(), "redact.json"))
              and not hasattr(events, "remember_redaction") and not hasattr(events, "redact_tagged"), r.stderr)

        # L4, L5: a lone surrogate in a spool file; invisible characters; tail and line separators
        sp = events.spool_dir()
        with open(os.path.join(sp, "20261117T130000.000000Z-sur.json"), "w") as fh:
            fh.write('{"family": "task", "type": "x_test", "message": "bad \\ud800 surrogate"}')
        n0 = len(local_log())
        mine = events.emit("task", "finished", "my own event after the spool")
        new = local_log()[n0:]
        check("L4 a spool file with a lone surrogate is recorded (replaced), and the caller's own event too, numbers in a row",
              mine and [e["type"] for e in new] == ["x_test", "finished"] and "�" in new[0]["message"]
              and new[1]["seq"] == new[0]["seq"] + 1, new)
        ev = events.emit("task", "finished", "a b c؜d⁠e­f\U000e0041g")
        check("L5 line and paragraph separators, U+061C, U+2060-2064, soft hyphens and tag characters are removed",
              ev["message"] == "a b c d e f g", repr(ev["message"]))
        with open(os.path.join(ENV["MCOMP_LOG_DIR"], "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 0, "time": "x", "family": "task", "type": "x", "vm": "v",
                                 "message": "tail-marker-one" + chr(0x2028) + "two"}, ensure_ascii=False) + "\n")
        out = cli("tail", "1").stdout
        check("L5 tail splits on newlines only, so a raw U+2028 inside a value doesn't hide the event",
              "tail-marker-one" in out, out)

        # L6: odd gateway records; one failing step doesn't stop the cycle
        st = {}
        events.add_blocked(st, [{"dst": "10.0.0.1:²", "reason": "a|b", "proto": "t|cp"}, "not a dict", {"dst": None}])
        bev = events.blocked_event(st, "testvm", now=time.time())
        check("L6 a '|' in a field and a port like '²' are counted, not fatal",
              bev and bev["data"]["destinations"][0]["port"] is None and bev["data"]["destinations"][0]["reason"] == "a|b", bev)

        class BadTail:
            def poll(self):
                raise RuntimeError("odd gateway record")
        with open(os.path.join(sp, "20261117T130001.000000Z-l6.json"), "w") as fh:
            json.dump({"family": "lifecycle", "type": "l6_test", "message": "still taken"}, fh)
        events.watch_cycle({}, BadTail(), "testvm")
        errs = open(os.path.join(events.state_dir(), "errors.log")).read()
        check("L6 a step that fails is noted and the others run", any(e["type"] == "l6_test" for e in local_log())
              and "blocked connections" in errs)

        # L7, nit: bad ports; a hand-edited URL is re-checked when sending
        r = sink_add("badport", "--kind", "webhook", "--url", "https://h.example:99999/x")
        check("L7 sink add refuses a port above 65535", r.returncode == 2 and "usable URL" in r.stderr, r.stderr)
        good_cfg = open(events.config_path()).read()
        cfg = json.loads(good_cfg)
        cfg["sinks"]["handbad"] = {"kind": "webhook", "url": "https://h.example:99999/x", "headers": [], "hmac_key": hk,
                                   "families": ["all"], "min_severity": "info"}
        cfg["sinks"]["plainhttp"] = {"kind": "webhook", "url": "http://example.com/x", "headers": [], "hmac_key": hk,
                                     "families": ["all"], "min_severity": "info"}
        events.write_private(events.config_path(), json.dumps(cfg))
        r = cli("sink", "list")
        events.emit("lifecycle", "test_url", "check the hand-edited URLs", budget=1)
        check("L7 sink list still works with a hand-edited bad port", r.returncode == 0 and "(unreadable URL)" in r.stdout,
              r.stdout + r.stderr)
        check("nit: a hand-edited plain-http URL off this Mac is refused when sending",
              "isn't allowed" in (stat_of("plainhttp")["last_error"] or "")
              and "isn't allowed" in (stat_of("handbad")["last_error"] or ""),
              (stat_of("plainhttp"), stat_of("handbad")))
        events.write_private(events.config_path(), good_cfg)
        events.remove_sink_files("handbad", "g0")
        events.remove_sink_files("plainhttp", "g0")

        # L8: one watcher per VM; its pidfile is private
        p1 = subprocess.Popen([PY, EV, "watch"], env=os.environ.copy(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        wait_for(lambda: events.watcher_pid() == p1.pid, 10)
        r = subprocess.run([PY, EV, "watch"], capture_output=True, text=True, timeout=30, env=os.environ.copy())
        check("L8 a second watcher for the same VM exits at once", r.returncode == 0 and "already running" in r.stderr
              and events.watcher_pid() == p1.pid, r.stderr)
        check("nit: the watcher's pidfile is 0600", oct(os.stat(events.watcher_pidfile()).st_mode & 0o777) == "0o600")
        p1.terminate()
        p1.wait(timeout=30)

        # L9: dot-files in the spool don't starve real ones
        for i in range(520):
            open(os.path.join(sp, f".tmp{i:04d}.json"), "w").close()
        with open(os.path.join(sp, "20261117T130002.000000Z-l9.json"), "w") as fh:
            json.dump({"family": "lifecycle", "type": "l9_test", "message": "not starved"}, fh)
        cli("drain")
        check("L9 520 temporary names don't hide a real spool file", any(e["type"] == "l9_test" for e in local_log()))
        for i in range(520):
            os.remove(os.path.join(sp, f".tmp{i:04d}.json"))

        # L10: events a bridge queued but couldn't send before exiting are recorded
        import queue as queue_mod
        saved = dict(events._async)
        events._async.update(q=queue_mod.Queue(), thread=object(), items=[])   # a worker that never runs
        e_new = events.emit_async("lifecycle", "controller_released", "left over at exit")
        e_taken = events.emit_async("lifecycle", "controller_released", "taken but not finished")
        events._async["items"][1]["state"] = "taken"                        # as if the worker was in the middle of it
        t0 = time.time()
        events.flush_async(0.2)
        events._async.clear()
        events._async.update(saved)
        left = {e.get("message"): e for e in local_log() if e.get("message") in ("left over at exit", "taken but not finished")}
        check("L10/R10 at exit, events the worker hadn't started and one it started but didn't finish are recorded, ids kept",
              set(left) == {"left over at exit", "taken but not finished"} and left["left over at exit"]["id"] == e_new["id"]
              and left["taken but not finished"]["id"] == e_taken["id"] and left["left over at exit"]["origin"] == "bridge"
              and time.time() - t0 < 5, left)

        # L12: a lost counter file doesn't restart numbering
        last = max(e["seq"] for e in local_log() if isinstance(e.get("seq"), int) and e["seq"] < 900000)
        sent = max(stat_of(n)["sent_seq"] for n in events.load_config()["sinks"])
        os.remove(os.path.join(events.state_dir(), "seq"))
        ev = events.emit("task", "finished", "after the counter was lost")
        events.write_private(os.path.join(events.state_dir(), "seq"), "3")     # an old copy put back
        ev2 = events.emit("task", "finished", "after an old counter came back")
        check("R11 a restored (older) counter: numbering still continues after the highest number anywhere",
              ev2["seq"] == ev["seq"] + 1, (ev["seq"], ev2["seq"]))
        check("L12 a lost seq file: numbering continues after what the log, backlogs and sinks have",
              ev["seq"] == max(last, sent) + 1, (ev["seq"], last, sent))

        # L13: a debounce mark only after the event is recorded
        orig, calls = events.record, []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            return orig(*a, **kw)
        events.record = flaky
        e1 = events.emit("needs_you", "other", "l13", debounce="l13")
        e2 = events.emit("needs_you", "other", "l13", debounce="l13")
        events.record = orig
        check("L13 an event that failed to record doesn't use up its debounce", e1 is None and e2 is not None)

        # L14: --replace says what it does
        sink_add("web2", "--kind", "webhook", "--url", web.url("/w2"), "--families", "lifecycle")
        r = tty_cli("sink", "add", "web2", "--kind", "webhook", "--url", web.url("/w2"), "--replace", "--yes")
        check("L14 --replace says the old signing key stops working and its backlog is dropped",
              r.returncode == 0 and "no longer works" in r.stdout and "dropped" in r.stdout, r.stdout + r.stderr)
        sink_remove("web2")

        # L15: origin, set by the code path
        refill()
        n0 = len(local_log())
        cli("emit", "lifecycle", "o_script", "--message", "from a script")
        cli("notify", "o_claude says hello", "--why", "other")
        with open(os.path.join(sp, "20261117T130003.000000Z-o.json"), "w") as fh:
            json.dump({"family": "lifecycle", "type": "o_spool", "message": "from the spool", "origin": "bridge"}, fh)
        cli("drain")
        events.emit("lifecycle", "o_bridge", "from a bridge", origin="bridge")
        events.emit("lifecycle", "o_watcher", "from the watcher", origin="watcher")
        org = {e["type"]: e.get("origin") for e in local_log()[n0:]}
        org["o_claude"] = next((e.get("origin") for e in local_log()[n0:] if e.get("message") == "o_claude says hello"), None)
        check("L15 origin is set by the code path (a spool file can't claim another)",
              org.get("o_script") == "script" and org.get("o_claude") == "claude" and org.get("o_spool") == "spool"
              and org.get("o_bridge") == "bridge"
              and org.get("o_watcher") == "watcher", org)
        out = cli("tail", "5").stdout
        lr = [lr for b in otlp.bodies() for rl_ in b["resourceLogs"] for sl in rl_["scopeLogs"] for lr in sl["logRecords"]][-1]
        check("L15 origin shows in tail, OTLP attributes and syslog structured data",
              " watcher " in out and any(a["key"] == "metacomputer.origin" for a in lr["attributes"])
              and (not sysrx or 'origin="' in sysrx.frames[-1]), out)

        # nits: ntfy ?auth=, otlp_url with a query, a 400 inside a batch, the byte cap, a slow backlog and sink test
        sink_add("ntfyauth", "--kind", "ntfy", "--url", ntfy.url("/t2?auth=tok123456"), "--families", "needs_you")
        events.emit("needs_you", "other", "auth check")
        check("nit: an ntfy ?auth= query is kept", any(p_ == "/?auth=tok123456" for p_, _, _ in ntfy.reqs), ntfy.reqs[-1][0])
        sink_remove("ntfyauth")
        otlp_url = events.sinkkit.registry.module("otlp").otlp_url      # sinks/otlp/sink.py
        check("nit: otlp_url puts /v1/logs before a query", otlp_url("https://h.example/x?y=1")
              == "https://h.example/x/v1/logs?y=1" and otlp_url("http://127.0.0.1:4318/v1/logs/")
              == "http://127.0.0.1:4318/v1/logs")
        refill()
        orx = HTTPRx()
        sink_add("otlp2", "--kind", "otlp", "--url", orx.url(), "--families", "task")
        orx.mode = "503"
        good1 = events.emit("task", "finished", "batch one")
        events.emit("task", "finished", "POISON")
        good3 = events.emit("task", "finished", "batch three")
        orx.mode = lambda body: 400 if b"POISON" in body else 200
        for _ in range(5):
            cli("drain", "--now")
        got = [json.loads(lr["body"]["stringValue"])["seq"] for b in orx.bodies() for rl_ in b["resourceLogs"]
               for sl in rl_["scopeLogs"] for lr in sl["logRecords"]]
        check("nit: one refused event in an OTLP batch costs only itself (retried one by one, then dropped)",
              got == [good1["seq"], good3["seq"]] and stat_of("otlp2")["dropped"] == 1, got)
        sink_remove("otlp2")
        orx.close()
        ev = events.emit("task", "finished", "bytes", data={f"k{i}": "é" * 300 for i in range(12)})
        check("nit: the 4 KB cap on data counts bytes", ev["data"].get("truncated") is True, list(ev["data"])[:3])
        slow = HTTPRx()
        sink_add("slow", "--kind", "webhook", "--url", slow.url(), "--families", "lifecycle")
        slow.mode = "503"
        for i in range(44):
            events.emit("lifecycle", "slow_fill", f"fill {i}", only_sinks={"slow"}, deliver_now=(i == 0), exempt=True)
        slow.mode, slow.delay = "ok", 0.25
        r = cli("sink", "test", "slow", timeout=120)
        check("nit: sink test waits for an earlier backlog that takes more than 10 s", r.returncode == 0
              and r.stdout.startswith("OK") and len(slow.reqs) == 45, (r.stdout, len(slow.reqs)))
        sink_remove("slow")
        slow.close()

        print("second review's fixes")
        refill()
        # R1: reserved types can't be emitted from outside; strict windows; the phone cap
        rs = [cli("emit", "needs_you", "suppressed", "--message", "x", "--debounce", "0"),
              cli("emit", "security", "blocked_connections", "--message", "x"),
              cli("emit", "security", "sink_added", "--message", "x"),
              cli("emit", "security", "sink_config_changed", "--message", "x"),
              cli("emit", "lifecycle", "test", "--message", "x"),
              cli("emit", "actions", "call", "--message", "x")]
        check("R1 events emit refuses reserved types: summaries, the watcher's, the bridge's, sink changes, sink tests, "
              "actions", all(r.returncode == 2 and "reserved" in r.stderr for r in rs), [r.stderr for r in rs])
        r = cli("_emit", "lifecycle", "x", "--message", "x")
        check("B5 there's no separate internal _emit any more (the hooks use emit, labelled as such)",
              r.returncode == 2 and "invalid choice" in r.stderr, r.stderr)
        rej0 = int(events.read_json(os.path.join(events.state_dir(), "spool.json"), {}).get("rejected", 0))
        for i, obj in enumerate([{"family": "security", "type": "sink_added", "message": "forged"},
                                 {"family": "needs_you", "type": "suppressed", "message": "forged"},
                                 {"family": "actions", "type": "call", "message": "forged"}]):
            with open(os.path.join(sp, f"20261117T140000.00000{i}Z-res.json"), "w") as fh:
                json.dump(obj, fh)
        cli("drain")
        rej1 = int(events.read_json(os.path.join(events.state_dir(), "spool.json"), {}).get("rejected", 0))
        check("R1 spool files can't claim reserved types either (quarantined)", rej1 - rej0 == 3
              and not any(e.get("message") == "forged" for e in local_log()), (rej0, rej1))
        wp = os.path.join(events.state_dir(), "test-window.json")
        t = 2_000_000.0
        r10 = [events._window(wp, "k", ((10, 600),), t + i, True)[0] for i in range(10)]
        w11 = events._window(wp, "k", ((10, 600),), t + 10, True)[0]
        w300 = events._window(wp, "k", ((10, 600),), t + 300, True)[0]
        w601 = events._window(wp, "k", ((10, 600),), t + 601, True)[0]
        check("R1 a strict sliding window: 10 in any 10 minutes, and no new burst after a pause",
              all(r10) and not w11 and not w300 and w601, (r10, w11, w300, w601))
        t = 3_000_000.0
        hour = [events._window(wp, "h", events.PHONE_LIMITS, t + 601 * (i // 10) + (i % 10), True)[0] for i in range(31)]
        check("R1 phones: also at most 30 in any hour", hour[:30] == [True] * 30 and hour[30] is False, hour[-3:])
        refill()
        prx = HTTPRx()
        sink_add("phone2", "--kind", "ntfy", "--url", prx.url("/p2-topic"))
        for i in range(6):
            events.emit("lifecycle", "r1_phone", f"phone {i}")
        for i in range(6):
            events.emit("task", "r1_phone", f"phone task {i}")
        r = cli("sink", "test", "phone2")
        check("R1 a phone sink gets at most 10 in 10 minutes whatever the family, sink tests included",
              len(prx.reqs) == 10 and r.returncode == 1 and "phone limit" in r.stdout, (len(prx.reqs), r.stdout))
        age_window("phonelimit.json", "phone2", 3601)
        events.emit("lifecycle", "r1_phone", "after the hour")
        cli("drain", "--now")                       # (the clock didn't really move: no waiting for the next slot)
        last4 = [b["message"] for b in prx.bodies()[-4:]]
        check("B1 what the phone's cap held back waits its turn and then arrives in order: nothing is dropped",
              len(prx.reqs) == 14 and last4[:2] == ["phone task 4", "phone task 5"] and last4[2].startswith("Test event")
              and last4[3] == "after the hour", last4)
        sink_remove("phone2")
        prx.close()
        base = len(act.reqs)
        for i in range(12):
            events.emit("actions", "call", f"tool {i}", data={"tool": "click"}, origin="bridge", exempt=True)
        check("R1 the relay's actions events (code path only) stay outside the family limit", len(act.reqs) - base == 12,
              len(act.reqs) - base)

        # R2: hand edits to events.json are noticed and announced
        refill()
        srx = HTTPRx()
        bw = len(web.changes)
        cfg = json.loads(open(events.config_path()).read())
        cfg["sinks"]["sneaky"] = {"kind": "webhook", "url": srx.url("/exfil-path-segment"), "headers": [],
                                  "hmac_key": hk, "families": ["all"], "min_severity": "info"}
        open(events.config_path(), "w").write(json.dumps(cfg))           # not through `events sink`
        events.emit("task", "finished", "after a hand edit")
        ch = [json.loads(x)["data"] for _, _, x in web.changes[bw:]]
        check("R2 a hand edit is announced (security.sink_config_changed, masked) to the sinks from before; not the new one",
              ch and ch[-1]["type"] == "sink_config_changed" and ch[-1]["data"]["added"][0]["sink"] == "sneaky"
              and "exfil-path-segment" not in json.dumps(ch[-1]) and not srx.changes, ch[-1:])
        events.emit("task", "finished", "after that")
        check("R2 once: the same edit isn't announced again; detection, not prevention (the edited sink is used)",
              len(web.changes) == bw + 1 and srx.reqs, (len(web.changes) - bw, len(srx.reqs)))
        cfg["sinks"].pop("sneaky")
        open(events.config_path(), "w").write(json.dumps(cfg))
        events.emit("task", "finished", "after undoing it")
        ch = [json.loads(x)["data"] for _, _, x in web.changes[bw:]]
        check("R2 taking it out by hand is announced too", ch[-1]["data"]["removed"][0]["sink"] == "sneaky", ch[-1:])
        events.remove_sink_files("sneaky", "g0")
        srx.close()

        # R3: a sink-change announcement outlives the change
        drx = HTTPRx()
        sink_add("gone", "--kind", "webhook", "--url", drx.url("/g"), "--families", "lifecycle")
        drx.mode = "503"
        r = sink_remove("gone")
        pend = events.admin_pending()
        check("R3 removing a sink that's down: its announcement is kept, with the old destination",
              r.returncode == 0 and any(p_["sink"] == "gone" and p_["type"] == "sink_removed" for p_ in pend)
              and "gone" not in events.load_config()["sinks"], pend)
        drx.mode = "ok"
        cli("drain", "--now")
        alog = open(os.path.join(events.state_dir(), "admin.log")).read()
        check("R3 it's delivered once the sink is back; nothing is left pending; the outcome is logged",
              [json.loads(x)["data"]["type"] for _, _, x in drx.changes] == ["sink_removed"]
              and not any(p_["sink"] == "gone" for p_ in events.admin_pending()) and "delivered to gone" in alog,
              alog[-300:])
        drx.close()
        dead2 = socket.socket()
        dead2.bind(("127.0.0.1", 0))
        dport = dead2.getsockname()[1]
        dead2.close()
        sink_add("dead2", "--kind", "otlp", "--url", f"http://127.0.0.1:{dport}", "--families", "lifecycle")
        sink_remove("dead2")
        for _ in range(events.ADMIN_MAX_ATTEMPTS):
            events.flush_admin(force=True)
        alog = open(os.path.join(events.state_dir(), "admin.log")).read()
        check("R3 a destination that never answers is given up after its attempts, and that's logged",
              not any(p_["sink"] == "dead2" for p_ in events.admin_pending()) and "gave up on dead2" in alog, alog[-300:])

        # F17: an OTLP collector's partial success: the rejected records are dropped (counted, with the
        # collector's message as last_error), not counted as delivered, and not retried
        refill()
        part = {"got": [], "reject": 1}

        class PartialH(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                recs_ = [r_ for rl in body.get("resourceLogs", []) for sl in rl.get("scopeLogs", []) for r_ in sl.get("logRecords", [])]
                part["got"].append(len(recs_))
                count = part["raw"] if "raw" in part else str(part["reject"])
                ans = part["body"] if "body" in part else json.dumps({"partialSuccess": {
                    "rejectedLogRecords": count, "errorMessage": "attribute too long \x1b[31m(red)"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(ans)))
                self.end_headers()
                self.wfile.write(ans)

            def log_message(self, *_):
                pass
        psrv = ThreadingHTTPServer(("127.0.0.1", 0), PartialH)
        psrv.daemon_threads = True
        threading.Thread(target=psrv.serve_forever, daemon=True).start()
        sink_add("partial", "--kind", "otlp", "--url", f"http://127.0.0.1:{psrv.server_address[1]}", "--families", "task")
        n0 = len(part["got"])
        events.emit("task", "finished", "a record the collector refuses")
        ps = stat_of("partial")
        check("F17 OTLP partial success: the rejected record counts as dropped, not delivered, with the collector's message",
              part["got"][n0:] == [1] and ps["delivered"] == 0 and ps["dropped"] == 1
              and "rejected 1 of 1" in (ps["last_error"] or "") and "attribute too long" in ps["last_error"]
              and "\x1b" not in ps["last_error"], (part["got"][n0:], ps))
        part["reject"] = 0
        events.emit("task", "finished", "one it accepts")
        ps2 = stat_of("partial")
        check("F17 ...and it isn't retried: the next request holds only the next event", part["got"][n0:] == [1, 1]
              and ps2["delivered"] == 1 and ps2["dropped"] == 1 and ps2["sent_seq"] > ps["sent_seq"], (part["got"][n0:], ps2))
        st_line = cli("status").stdout
        st_list = cli("sink", "list").stdout
        check("F17 events status and sink list show the rejection", "rejected 1 of 1" in st_line and "partial (1:" in st_line
              and "refused by the receiver" in st_list, (st_line[-600:], st_list[-600:]))
        # A partial success whose count of refused records isn't a non-negative integer is an error reply: nothing is
        # counted as delivered or dropped and the events are sent again (true, 2.7, -1, "x" were read as 0, everything
        # delivered; "+3" as 3 refused, dropped)
        refill()
        odd = []
        for raw in (True, 2.7, -1, "x", "+3", False):
            part["raw"] = raw
            s0, n0 = stat_of("partial"), len(part["got"])
            events.emit("task", "finished", f"an answer whose count is {raw!r}")
            events.flush_sink("partial", events.load_config()["sinks"]["partial"], force=True)
            s1 = stat_of("partial")
            odd.append((raw, len(part["got"]) > n0, s1["delivered"] - s0["delivered"], s1["dropped"] - s0["dropped"],
                        s1["sent_seq"] == s0["sent_seq"], s1["last_error"]))
        part.pop("raw")
        part["reject"] = 0
        s0 = stat_of("partial")
        events.flush_sink("partial", events.load_config()["sinks"]["partial"], force=True)
        s1 = stat_of("partial")
        check("F17 an answer whose count of refused records isn't a whole number (true, 2.7, -1, \"x\", \"+3\", false) "
              "is an error reply: not delivered, nothing dropped, sent again; then a good answer takes all six",
              all(asked and dd == 0 and dr == 0 and same and "can't be read" in (err or "") for _, asked, dd, dr, same, err in odd)
              and s1["delivered"] - s0["delivered"] == 6 and s1["dropped"] == s0["dropped"], (odd, s0, s1))
        # ...and so is an answer that can't be read at all (it counted as everything delivered): a count too long to
        # convert (5,000 digits) or too large for a double (1e999), a body that isn't JSON or isn't an object. An empty
        # body (OTLP's empty response, as protobuf encodes it), only whitespace or a null partialSuccess is a full success.
        refill()

        def answer_with(body):
            part["body"] = body
            s0, n0 = stat_of("partial"), len(part["got"])
            events.emit("task", "finished", "an answer of " + repr(body[:40]))
            events.flush_sink("partial", events.load_config()["sinks"]["partial"], force=True)
            s1 = stat_of("partial")
            return (len(part["got"]) > n0, s1["delivered"] - s0["delivered"], s1["dropped"] - s0["dropped"],
                    s1["sent_seq"] == s0["sent_seq"], s1["last_error"])
        unread = {b[:30]: answer_with(b) for b in (b'{"partialSuccess": {"rejectedLogRecords": ' + b"9" * 5000 + b"}}",
                                                   b'{"partialSuccess": {"rejectedLogRecords": 1e999}}', b"not json", b"[1]")}
        part["body"] = b"{}"
        s0 = stat_of("partial")
        events.flush_sink("partial", events.load_config()["sinks"]["partial"], force=True)
        s1 = stat_of("partial")
        fine = {b: answer_with(b) for b in (b"", b" \r\n", b'{"partialSuccess": null}')}
        part.pop("body")
        check("F17 an answer that can't be read (a count of 5,000 digits or 1e999, a body that isn't JSON or isn't an "
              "object) is an error reply: not delivered, nothing dropped, sent again; an empty body, whitespace or a null "
              "partialSuccess is a full success",
              all(asked and dd == 0 and dr == 0 and same and "can't be read" in (err or "")
                  for asked, dd, dr, same, err in unread.values())
              and s1["delivered"] - s0["delivered"] == 4 and s1["dropped"] == s0["dropped"]
              and all(asked and dd == 1 and dr == 0 and not same for asked, dd, dr, same, _ in fine.values()),
              (unread, s0["delivered"], s1["delivered"], fine))
        sink_remove("partial")
        psrv.shutdown()
        psrv.server_close()

        # F18: delivery has a wall-clock deadline that a sink sending one byte a second can't stretch
        refill()
        trickle_stop = threading.Event()

        def trickle_conn(c_):
            try:
                c_.settimeout(5)
                buf_ = b""
                while b"\r\n\r\n" not in buf_:
                    d_ = c_.recv(65536)
                    if not d_:
                        return
                    buf_ += d_
                for ch in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nX-Slow: " + b"z" * 60:
                    if trickle_stop.is_set():
                        break
                    c_.sendall(bytes([ch]))
                    time.sleep(1.0)
            except OSError:
                pass
            finally:
                c_.close()
        tsock = socket.socket()
        tsock.bind(("127.0.0.1", 0))
        tsock.listen(16)

        def trickle_loop():
            while not trickle_stop.is_set():
                try:
                    c_, _ = tsock.accept()
                except OSError:
                    return
                threading.Thread(target=trickle_conn, args=(c_,), daemon=True).start()
        threading.Thread(target=trickle_loop, daemon=True).start()
        turl = f"http://127.0.0.1:{tsock.getsockname()[1]}/"
        t0 = time.time()
        ok_, err_, _, _ = events.deliver({"kind": "webhook", "url": turl, "hmac_key": base64.b64encode(b"k" * 32).decode()},
                                         [events.build_event("task", "finished", "to a trickling sink")], 1.5)
        dt = time.time() - t0
        check("F18 deliver() to a sink that trickles a byte a second ends at its deadline (1.5 s), timed out",
              not ok_ and err_ == "timed out" and dt < 2.6, (ok_, err_, round(dt, 1)))
        sink_add("trickle", "--kind", "webhook", "--url", turl, "--families", "task")
        r = sink_remove("trickle")      # its announcement goes to the trickling sink too, and waits there
        pend = [p_ for p_ in events.admin_pending() if p_["sink"] == "trickle"]
        check("F18 sink remove with a trickling destination: done within its budget, the announcement kept for later",
              r.returncode == 0 and pend, (r.returncode, pend, r.stderr[-300:]))
        t0 = time.time()
        events.flush_admin(budget=2.0, force=True)
        dt1 = time.time() - t0
        t0 = time.time()
        events.emit("lifecycle", "started", "an ordinary command while an announcement waits on a trickling sink")
        dt2 = time.time() - t0
        check("F18 flush_admin and an ordinary emit (start, stop) return on time while that sink trickles",
              dt1 < 3.6 and dt2 < events.EMIT_BUDGET + 2.0, (round(dt1, 1), round(dt2, 1)))
        trickle_stop.set()
        tsock.close()
        for _ in range(events.ADMIN_MAX_ATTEMPTS):
            events.flush_admin(force=True)

        # F7: the relay's per-record check reads events.json only when it changes. What's counted is every parse on the
        # call path but the redaction list's own (known_secrets: the values to hide, read again when it's 30 s old,
        # whatever the cache does), which is made due in both halves here: under load the suite reaches this check at a
        # different point of those 30 s, and that parse was counted too ((1, 1) or (0, 2)).
        calls_ = [0]
        real_load = events.load_config

        def counting():
            # the call path, not the sending thread; not the redaction list's refresh
            if threading.current_thread() is threading.main_thread() and sys._getframe(1).f_code.co_name != "known_secrets":
                calls_[0] += 1
            return real_load()
        events.load_config = counting
        try:
            events.config_for_reading()
            calls_[0] = 0
            events._known["at"] = 0                 # the redaction list is due: it reads events.json in this half
            for i_ in range(50):
                events.emit_async("actions", "call", f"record {i_}", data={"seq": i_})
            unchanged = calls_[0]
            sink_add("acts", "--kind", "webhook", "--url", web.url("/acts"), "--families", "actions")
            calls_[0] = 0
            events._known["at"] = 0                 # ...and in this one
            events.emit_async("actions", "call", "after a change")
            changed = calls_[0]
        finally:
            events.load_config = real_load
            events.flush_async()
        check("F7 50 action-log records with events.json unchanged: parsed 0 times; once after it changes (the redaction "
              "list's own refresh aside, due in both)", unchanged == 0 and changed == 1, (unchanged, changed))
        sink_remove("acts")

        # R4: sink generations; replacing waits for a sender
        refill()
        orx, nrx = HTTPRx(), HTTPRx()
        sink_add("gtest", "--kind", "webhook", "--url", orx.url("/old"), "--families", "lifecycle")
        old_def = events.load_config()["sinks"]["gtest"]
        orx.mode = "503"
        events.emit("lifecycle", "g_one", "queued for the old one")
        lk = events.Lock(events.sink_lock("gtest"), 1)
        lk.__enter__()                              # as if a sender were busy with gtest's backlog
        m_, s_ = pty.openpty()
        pr = subprocess.Popen([PY, EV, "sink", "add", "gtest", "--kind", "webhook", "--url", nrx.url("/new"),
                               "--families", "lifecycle", "--replace", "--yes"], stdin=s_, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=os.environ.copy())
        time.sleep(1.5)
        waited = events.gen_of(events.load_config()["sinks"]["gtest"]) == events.gen_of(old_def)
        lk.__exit__()
        pr.wait(timeout=60)
        os.close(s_)
        os.close(m_)
        new_def = events.load_config()["sinks"]["gtest"]
        check("R4 replacing a sink waits while a sender holds its lock, then makes a new generation",
              waited and pr.returncode == 0 and events.gen_of(new_def) != events.gen_of(old_def), pr.returncode)
        orx.mode = "ok"
        st_old = events.flush_sink("gtest", old_def, force=True)
        check("R4 a sender holding the retired generation sends nothing to the old URL; the old files are gone",
              st_old is None and not orx.reqs and not os.path.exists(events.sink_file("gtest", events.gen_of(old_def), "queue"))
              and os.path.exists(events.sink_lock("gtest")), (st_old, len(orx.reqs)))
        events.emit("lifecycle", "g_two", "for the new one")
        check("R4 the new generation gets new events at the new URL", [b["data"]["type"] for b in nrx.bodies()] == ["g_two"],
              [b["data"]["type"] for b in nrx.bodies()])
        sink_remove("gtest")
        orx.close()
        nrx.close()

        # LK: the state lock: a second holder waits its time, then gets Busy with its file closed; leaving unlocks
        lkp = os.path.join(events.state_dir(), "lktest.lock")
        held = events.Lock(lkp, 1)
        held.__enter__()
        n_fd, t0 = len(os.listdir("/dev/fd")), time.monotonic()
        try:
            with events.Lock(lkp, 0.3):
                got = "acquired"
        except events.Busy as e:
            got = e
        waited, fd_delta = time.monotonic() - t0, len(os.listdir("/dev/fd")) - n_fd
        check("LK a second holder waits its time, then gets Busy naming the lock, with its file closed",
              type(got) is events.Busy and str(got) == lkp and 0.3 <= waited < 1.5 and fd_delta == 0, (got, waited, fd_delta))
        held.__exit__()
        with events.Lock(lkp, 0.3) as lk2:
            took = lk2.fd is not None
        check("LK leaving the lock unlocks it: the next holder gets it at once, and leaving closes its file",
              held.fd is None and took and lk2.fd is None, (held.fd, took, lk2.fd))

        # R5: text from inside the VM stays out of messages
        src = open(os.path.join(LAB, "bin", "metacomputer")).read()
        fn = re.search(r"^      rec_fail\(\) \{.*?^      \}$", src, re.M | re.S).group(0)
        n0 = len(local_log())
        cfgf = os.path.join(TD, "test.env")
        with open(cfgf, "w") as fh:
            fh.write(f'VM_NAME=testvm\nRUN_DIR="{ENV["MCOMP_RUN_DIR"]}"\nLOG_DIR="{ENV["MCOMP_LOG_DIR"]}"\n'
                     f'SSH_DIR="{ENV["MCOMP_SSH_DIR"]}"\n')
        guest_said = "CALL +1 555 0100 now https://evil.example/x?token=abc"
        rr = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; die() {{ exit 1; }}; name=demo; mode=guest; '
                             f'state=/nonexistent/x\n{fn}\nrec_fail "the VM\'s recorder didn\'t hand over a video" '
                             '"the VM\'s recorder said: $1" "$1"', "x", guest_said],
                            capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfgf), timeout=60,
                            stdin=subprocess.DEVNULL)
        new = [e for e in local_log()[n0:] if e["type"] == "recording_failed"]
        # The guest's words are looked for everywhere except the event's own random id, sequence and time, which can
        # contain "555" by chance (a uuid like c26b4358-555d-...).
        said = json.dumps({k: v for k, v in new[0].items() if k not in ("id", "seq", "time")}) if new else ""
        check("R5 a failed recording: a fixed message and a fixed reason; nothing the VM said, even cleaned",
              new and new[0]["message"] == "Recording 'demo' failed: the VM's recorder didn't hand over a video"
              and new[0]["data"].get("reason") == "other" and "detail" not in new[0]["data"]
              and "CALL" not in said and "555" not in said and "evil.example" not in said, (rr.stderr, new))
        n0 = len(local_log())
        subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; die() {{ exit 1; }}; name=demo; mode=guest; '
                        f'state=/nonexistent/x\n{fn}\nrec_fail "the VM\'s recorder didn\'t hand over a video" "x" recorder_answer'],
                       capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfgf), timeout=60,
                       stdin=subprocess.DEVNULL)
        new = [e for e in local_log()[n0:] if e["type"] == "recording_failed"]
        check("R5 ...a known reason is passed as its category", new and new[0]["data"].get("reason") == "recorder_answer",
              new)
        f5 = untrusted_findings("https://start.example/", [], [{"tabs": [("https://call-555-now.evil.example/x", "t")],
                                                                  "password_fields": 1}],
                                "http://we ird.example/", 1, ["https://a.example/"], ["invoice.pdf.exe"])
        check("R5 open-untrusted: the summary is fixed words, odd hosts aren't origins, download names aren't sent",
              f5 and "example" not in f5["summary"] and "download_names" not in f5
              and f5["final_origin"] is None and "invoice" not in json.dumps(f5), f5)
        st5 = {}
        events.add_blocked(st5, [{"dst": "evil-text.example:80", "reason": "say: call now!", "proto": "tcp"}])
        b5 = events.blocked_event(st5, "testvm", now=time.time() + 10**6)
        check("R5 a blocked destination that isn't an address, or an odd reason, stays out of the message",
              "evil-text" not in b5["message"] and "call now" not in b5["message"] and "a destination" in b5["message"],
              b5["message"])

        # R6: more link shapes
        cases6 = {"https://safe.example/,https://alice:SECRET@other.example/ x": "https://safe.example/,https://other.example/ x",
                  "go 127.0.0.1/reset#SECRET now": "go 127.0.0.1/reset now",
                  "go localhost/reset?SECRET now": "go localhost/reset now",
                  "go [::1]:8080/x?SECRET now": "go [::1]:8080/x now",
                  "alice:SECRET@other.example/x": "other.example/x",
                  "url=alice:SECRET@h.example/p": "url=h.example/p",
                  "see https%3A%2F%2Falice%3ASECRET%40h.example%2Fp%3Ftoken%3DSECRET2 end": "see https://h.example/p end",
                  "mailto:bob@y.example": "mailto:bob@y.example"}
        wrong = {c: events.clean_str(c, 500) for c in cases6 if events.clean_str(c, 500) != cases6[c]}
        check("R6 every link in a word; bare IPv4, [IPv6] and localhost; credentials anywhere; encoded links",
              not wrong, wrong)

        # R8: key names in any style
        ev = events.emit("task", "finished", "keys2", data={"signingKey": 1, "access-key": 2, "pa" + chr(0x200B) + "ssword": 3,
                                                             "API.Key": 4, "x-auth-token": 5, "Session-ID": 6, "keyMoments": 7})
        check("R8 camelCase, hyphenated, dotted and invisible-character key names are recognised",
              set(ev["data"]) == {"keyMoments"}, ev["data"])

        # R9: a spool file that can't be parsed (too deep) doesn't block the spool; folders don't count
        for i in range(510):
            os.mkdir(os.path.join(sp, f"0000{i:04d}.json"))
        with open(os.path.join(sp, "0001-deep.json"), "w") as fh:
            fh.write("[" * 30000 + "]" * 30000)
        with open(os.path.join(sp, "0002-valid.json"), "w") as fh:
            json.dump({"family": "lifecycle", "type": "r9_test", "message": "after the deep one"}, fh)
        cli("drain")
        check("R9 a RecursionError is caught and the file quarantined; 510 folders don't use up the 500",
              any(e["type"] == "r9_test" for e in local_log()) and "0001-deep.json" in os.listdir(os.path.join(sp, ".quarantine"))
              and not os.path.exists(os.path.join(sp, "0001-deep.json")))
        for i in range(510):
            os.rmdir(os.path.join(sp, f"0000{i:04d}.json"))

        # R12: provenance on the phone
        refill()
        base = len(ntfy.reqs)
        cli("emit", "needs_you", "other", "--message", "scripted ping")
        with open(os.path.join(sp, "20261117T150000.000000Z-r12.json"), "w") as fh:
            json.dump({"family": "needs_you", "type": "r12_spool", "message": "spooled ping"}, fh)
        cli("drain")
        msgs = [b["message"] for b in ntfy.bodies()[base:]]
        check("R12 on the phone, events anything could have sent say where they came from",
              any(m_.startswith("[sent from the command line] scripted ping") for m_ in msgs)
              and any(m_.startswith("[from another program] spooled ping") for m_ in msgs), msgs)

        # R13: syslog counts a reset or a TLS error at close as a failure
        if sysrx:
            s2 = SyslogRx(certs[1], certs[2])
            s2.mode = "reset"
            sink_add("siem2", "--kind", "syslog", "--url", f"syslog+tls://localhost:{s2.port}", "--ca", certs[0],
                     "--families", "lifecycle")
            e13 = events.emit("lifecycle", "r13", "over syslog")
            st13 = stat_of("siem2")
            check("R13 syslog: a connection reset at close is a failure, kept and retried",
                  st13["last_error"] and st13["sent_seq"] < e13["seq"], st13)
            s2.mode = "ok"
            cli("drain", "--now")
            check("R13 once the server closes cleanly, it's delivered", stat_of("siem2")["sent_seq"] >= e13["seq"],
                  stat_of("siem2"))
            sink_remove("siem2")

        print("third review's fixes")
        refill()
        # B1: the phone cap applies when sending: retries count, an outage's backlog doesn't go at once,
        # announcements too (coalesced)
        p3 = HTTPRx()
        sink_add("ph3", "--kind", "ntfy", "--url", p3.url("/ph3-topic"), "--families", "lifecycle")
        p3.mode = "503"
        events.emit("lifecycle", "b1", "first", exempt=True)
        for _ in range(3):
            cli("drain", "--now")

        def slots(name):
            return len((events.read_json(events.phone_window(), {}).get(name) or {}).get("times") or [])
        check("B1 every attempt to a phone takes a slot, retries included", slots("ph3") == 4, slots("ph3"))
        for _ in range(8):
            cli("drain", "--now")
        st3 = stat_of("ph3")
        check("B1 when the phone's slots are used up, nothing more is tried: it waits (not a failure)",
              slots("ph3") == 10 and st3.get("waiting") == "phone limit" and st3["next_retry"] > time.time(), (slots("ph3"), st3))
        sink_remove("ph3")
        p3.close()
        refill()
        p4 = HTTPRx()
        sink_add("ph4", "--kind", "ntfy", "--url", p4.url("/ph4-topic"), "--families", "lifecycle")
        p4.mode = "503"
        for i in range(14):
            events.emit("lifecycle", "b1_outage", f"during the outage {i}", exempt=True)
        p4.mode = "ok"
        cli("drain", "--now")
        got4 = [b["message"] for b in p4.bodies()]
        check("B1 after an outage the backlog doesn't arrive at once: the phone gets what its window allows, the rest waits",
              len(got4) == 9 and got4[0] == "during the outage 0" and stat_of("ph4").get("waiting") == "phone limit",
              (len(got4), got4[:2]))
        sink_remove("ph4")
        p4.close()
        refill()
        p5 = HTTPRx()
        sink_add("ph5", "--kind", "ntfy", "--url", p5.url("/ph5-topic"), "--families", "needs_you")
        p5.mode = "503"
        sink_add("dum1", "--kind", "webhook", "--url", web.url("/dum1"), "--families", "lifecycle")
        sink_remove("dum1")
        sink_add("dum2", "--kind", "webhook", "--url", web.url("/dum2"), "--families", "lifecycle")
        p5.mode = "ok"
        cli("drain", "--now")
        msgs5 = [json.loads(x)["message"] for _, _, x in p5.reqs + p5.changes]
        check("B1 sink-change announcements to a phone go under its cap, several waiting at once as one message",
              len(msgs5) == 1 and msgs5[0].startswith("3 changes to your event sinks"), msgs5)
        sink_remove("dum2")
        for i in range(10):
            events._window(events.phone_window(), "ph5", events.PHONE_LIMITS, time.time(), True)
        n5 = len(p5.reqs) + len(p5.changes)
        sink_add("dum3", "--kind", "webhook", "--url", web.url("/dum3"), "--families", "lifecycle")
        pend5 = [x for x in events.admin_pending() if x["sink"] == "ph5"]
        check("B1 when the phone has no room, the announcement waits for it without using up its attempts",
              len(p5.reqs) + len(p5.changes) == n5 and pend5 and all(x["attempts"] == 0 for x in pend5), pend5)
        sink_remove("dum3")
        sink_remove("ph5")
        p5.close()

        # B2: a broken events.json; the journal and its recovery
        refill()
        good_text = open(events.config_path()).read()
        bw, n0 = len(web.changes), len(local_log())
        open(events.config_path(), "w").write("{not json")
        cli("emit", "task", "b2_broken", "--message", "while the configuration is broken")
        line = cli("status", "--line").stdout
        ch = [json.loads(x)["data"] for _, _, x in web.changes[bw:]]
        check("B2 a broken events.json: events still reach the local log, status says why nothing is sent, and the "
              "last known sinks are told", any(e.get("type") == "b2_broken" for e in local_log()[n0:])
              and "can't be read" in line and ch and ch[-1]["type"] == "sink_config_changed"
              and ch[-1]["data"]["readable"] is False, (line, ch[-1:]))
        open(events.config_path(), "w").write(good_text)
        bw = len(web.changes)
        events.emit("task", "finished", "after putting it back")
        check("B2 putting the same sinks back is taken quietly, and delivery resumes",
              len(web.changes) == bw and "can't be read" not in cli("status", "--line").stdout)
        jr = HTTPRx()
        cur_hash = events._config_file_hash()
        target = {"kind": "webhook", "url": jr.url("/j"), "headers": [], "hmac_key": hk, "families": ["all"]}
        with events.global_lock():
            _, pa = events.journal_announcement("sink_added", "prepared and written", {}, {"jt": target},
                                                {"sinks": {}}, state="prepared", intended_hash=cur_hash)
            _, pb = events.journal_announcement("sink_added", "prepared, never written", {}, {"jt": target},
                                                {"sinks": {}}, state="prepared", intended_hash="0" * 64)
        events.flush_admin(force=True)
        got_j = [json.loads(x)["data"]["message"] for _, _, x in jr.changes]
        check("B2 recovery: a prepared announcement whose change was written is sent; one whose change wasn't is dropped",
              got_j == ["prepared and written"] and not os.path.exists(pb) and "never written; dropped"
              in open(os.path.join(events.state_dir(), "admin.log")).read(), got_j)
        orig_save = events.save_config

        def failing_save(*a, **kw):
            raise OSError("disk full")
        events.save_config = failing_save
        try:
            events.change_sink("ghost", {"kind": "webhook", "url": jr.url("/ghost"), "headers": [], "hmac_key": hk,
                                         "families": ["all"], "generation": "ghost1"}, None)
            interrupted = False
        except OSError:
            interrupted = True
        events.save_config = orig_save
        prepared = [r for _, r in events._admin_records() if r.get("state") == "prepared"]
        events.recover_journal()
        check("B2 a change interrupted before its file is written leaves a prepared announcement that recovery drops",
              interrupted and prepared and not [r for _, r in events._admin_records() if r.get("state") == "prepared"]
              and "ghost" not in events.load_config()["sinks"], (interrupted, len(prepared)))
        jr.close()
        orig_flush = events.flush_admin
        events.flush_admin = lambda *a, **kw: None      # as if it crashed right after journaling
        cfg = json.loads(open(events.config_path()).read())
        cfg["sinks"]["b2hand"] = {"kind": "webhook", "url": web.url("/b2hand"), "headers": [], "hmac_key": hk,
                                  "families": ["lifecycle"], "min_severity": "info"}
        open(events.config_path(), "w").write(json.dumps(cfg))
        events.config_guard()
        journaled = [r for _, r in events._admin_records() if (r.get("event") or {}).get("type") == "sink_config_changed"]
        events.flush_admin = orig_flush
        bw = len(web.changes)
        cli("drain", "--now")
        check("B2 a hand edit is journaled before it's taken as known, so it's delivered later even if sending was cut short",
              journaled and any(json.loads(x)["data"]["type"] == "sink_config_changed" for _, _, x in web.changes[bw:]),
              len(journaled))
        cfg["sinks"].pop("b2hand")
        open(events.config_path(), "w").write(json.dumps(cfg))
        events.config_guard()
        events.remove_sink_files("b2hand", "g0")

        # B3: concurrent sink changes don't act on a stale answer
        def tty_popen(*args):
            m_, s_ = pty.openpty()
            pr = subprocess.Popen([PY, EV, *args], stdin=s_, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=os.environ.copy())
            return pr, m_, s_
        pr, m_, s_ = tty_popen("sink", "add", "race1", "--kind", "webhook", "--url", web.url("/race-a"))
        time.sleep(1.0)                              # A is waiting for its answer
        sink_add("race1", "--kind", "webhook", "--url", web.url("/race-b"))
        os.write(m_, b"y\n")
        out_a, err_a = pr.communicate(timeout=60)
        os.close(s_)
        os.close(m_)
        check("B3 an add confirmed after someone else added the same name is refused (no silent replace)",
              pr.returncode == 2 and "changed while you were answering" in err_a
              and events.load_config()["sinks"]["race1"]["url"].endswith("/race-b"), err_a)
        pr, m_, s_ = tty_popen("sink", "add", "race1", "--kind", "webhook", "--url", web.url("/race-c"), "--replace")
        time.sleep(1.0)
        sink_add("race1", "--kind", "webhook", "--url", web.url("/race-d"), "--replace")
        os.write(m_, b"y\n")
        out_a, err_a = pr.communicate(timeout=60)
        os.close(s_)
        os.close(m_)
        check("B3 a replace confirmed after the sink was replaced again is refused too",
              pr.returncode == 2 and "changed while you were answering" in err_a
              and events.load_config()["sinks"]["race1"]["url"].endswith("/race-d"), err_a)
        sink_remove("race1")

        # B4: the spool can't be turned against us with FIFOs or links swapped in after the checks
        import threading as _th
        sp4 = events.spool_dir()
        os.mkfifo(os.path.join(sp4, "zz-fifo.json"))
        orig_entries = events._spool_entries
        events._spool_entries = lambda dfd: ["zz-fifo.json"]      # as if it was a regular file when listed
        done = []
        th = _th.Thread(target=lambda: done.append(events.drain_spool(events.load_config())), daemon=True)
        with events.global_lock():
            pass
        th.start()
        th.join(10)
        events._spool_entries = orig_entries
        check("B4 a FIFO swapped in after the listing doesn't block (opened without waiting, checked on the descriptor)",
              done and done[0] == (0, 1) and not os.path.exists(os.path.join(sp4, "zz-fifo.json")), done)
        outside = os.path.join(TD, "outside")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "real.json"), "w") as fh:
            json.dump({"family": "lifecycle", "type": "b4_link", "message": "through a link"}, fh)
        os.symlink(os.path.join(outside, "real.json"), os.path.join(sp4, "zz-link.json"))
        events._spool_entries = lambda dfd: ["zz-link.json"]
        n0 = len(local_log())
        events.drain_spool(events.load_config())
        events._spool_entries = orig_entries
        check("B4 a link swapped in isn't followed: refused, and its target left alone",
              not any(e["type"] == "b4_link" for e in local_log()[n0:]) and os.path.exists(os.path.join(outside, "real.json")))
        qd = os.path.join(sp4, ".quarantine")
        shutil.rmtree(qd, ignore_errors=True)
        os.symlink(outside, qd)
        with open(os.path.join(sp4, "zz-bad.json"), "w") as fh:
            fh.write("not json")
        cli("drain")
        check("B4 a .quarantine that is a link isn't used: the bad file stays in the spool (a file that can't be "
              "quarantined is never deleted), nothing lands outside",
              sorted(os.listdir(outside)) == ["real.json"] and os.path.exists(os.path.join(sp4, "zz-bad.json")),
              os.listdir(outside))
        os.remove(os.path.join(sp4, "zz-bad.json"))
        os.remove(qd)

        # B5: a trailing newline can't slip past the checks; Claude's own messages say so on a phone
        r1 = cli("emit", "lifecycle", "health\n", "--message", "x")
        r2 = sink_add("bad\n", "--kind", "webhook", "--url", web.url("/n"))
        check("B5 a type or sink name with a trailing newline is refused", r1.returncode == 2 and r2.returncode == 2,
              (r1.stderr, r2.stderr))
        refill()
        base = len(ntfy.reqs)
        cli("notify", "B5 sign in please", "--why", "login")
        msgs = [b["message"] for b in ntfy.bodies()[base:]]
        check("B5 on a phone, notify says it's from Claude", any(m_.startswith("[from Claude] B5 sign in please") for m_ in msgs),
              msgs)

        # B6: the watcher stays while announcements wait, even with no sinks left
        iso = dict(os.environ, MCOMP_EVENTS_DIR=os.path.join(TD, "iso"), MCOMP_RUN_DIR=os.path.join(TD, "iso-run"),
                   MCOMP_EVENTS_SPOOL=os.path.join(TD, "iso-spool"))
        r_none = cli("needs-watcher", env=iso)
        wpr = subprocess.Popen([PY, EV, "watch"], env=iso, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exited = wpr.wait(timeout=20) is not None
        os.makedirs(os.path.join(TD, "iso", "events-state", "admin"), exist_ok=True)
        with open(os.path.join(TD, "iso", "events-state", "admin", "000000000000001-00000000-0000-4000-8000-000000000001.json"), "w") as fh:
            json.dump({"event": {"type": "sink_removed", "seq": 1, "id": "x", "message": "m", "family": "security",
                                 "severity": "warn", "vm": "v", "host": "h", "time": events.now_iso(), "origin": "cli"},
                       "state": "committed", "targets": {"gone": {"sink": {"kind": "otlp", "url": f"http://127.0.0.1:{dport}"},
                                                                  "attempts": 0, "next": time.time() + 3600,
                                                                  "state": "pending"}}}, fh)
        r_pend = cli("needs-watcher", env=iso)
        wpr = subprocess.Popen([PY, EV, "watch"], env=iso, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)
        alive = wpr.poll() is None
        wpr.terminate()
        wpr.wait(timeout=30)
        check("B6 the watcher is needed (and stays) while an announcement waits, even with no sinks; not otherwise",
              r_none.returncode == 1 and exited and r_pend.returncode == 0 and alive, (r_none.returncode, r_pend.returncode, alive))

        # B7: decoding comes before the decisions
        ev = events.emit("task", "finished", "https://example.test/?a=x token%3DNEWSECRET end",
                         data={"pa%73sword": "hunter2", "ok": 1})
        check("B7 an encoded secret key name is recognised, and an encoded token= after a query is cut",
              "pa%73sword" not in ev["data"] and "password" not in ev["data"] and "NEWSECRET" not in ev["message"]
              and ev["data"].get("ok") == 1, (ev["message"], ev["data"]))

        # B8: an earlier build's live-view verifier is removed
        rp = os.path.join(events.state_dir(), "redact.json")
        with open(rp, "w") as fh:
            json.dump({"vnc": {"len": 8, "key": "00" * 16, "tag": "abcdef"}}, fh)
        events.emit("task", "finished", "after upgrading")
        check("B8 events-state/redact.json from an earlier build is deleted, and that's noted",
              not os.path.exists(rp) and "redact.json" in open(os.path.join(events.state_dir(), "errors.log")).read())

        print("CLI and the bash helper")
        refill()
        r = cli("tail", "3")
        check("events tail prints the newest events", r.returncode == 0 and len(r.stdout.splitlines()) == 3, r.stdout)
        r = cli("status", "--line")
        check("events status --line", r.returncode == 0 and re.match(r"\d+ sinks, watcher not running, backlog \d+", r.stdout),
              r.stdout)
        cfgf = os.path.join(TD, "test.env")
        with open(cfgf, "w") as fh:
            fh.write(f'VM_NAME=testvm\nRUN_DIR="{ENV["MCOMP_RUN_DIR"]}"\nLOG_DIR="{ENV["MCOMP_LOG_DIR"]}"\n'
                     f'SSH_DIR="{ENV["MCOMP_SSH_DIR"]}"\n')
        base = len(web.reqs)
        r = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; emit_event lifecycle checkpoint info '
                            '"Checkpoint x saved" "" name=x n=3; echo rc=$?'],
                           capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfgf), timeout=60)
        got = web.bodies()[base:]
        check("emit_event (bash) sends an event with its fields", "rc=0" in r.stdout and got
              and got[0]["data"]["type"] == "checkpoint" and got[0]["data"]["data"] == {"name": "x", "n": 3}, (r.stdout, r.stderr, got))
        good_cfg = open(events.config_path()).read()
        open(events.config_path(), "w").write("{not json")
        r = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; emit_event lifecycle stopped info "x" ""; echo rc=$?'],
                           capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfgf), timeout=60)
        check("a broken events configuration never fails the command", "rc=0" in r.stdout and not r.stderr, r.stdout + r.stderr)
        open(events.config_path(), "w").write(good_cfg)
        os.chmod(events.config_path(), 0o600)
        mc = os.path.join(LAB, "bin", "metacomputer")
        menv = dict(os.environ, MCOMP_CONFIG=cfgf)
        trap = next(x for x in open(mc).read().splitlines() if x.startswith('if [ "$cmd" = secrets ]'))
        rcs, n0 = [], len(local_log())
        for code in (3, 0, 1):
            rr = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; cmd=secrets; {trap}\nexit {code}'],
                                capture_output=True, text=True, env=menv, timeout=60, stdin=subprocess.DEVNULL)
            rcs.append(rr.returncode)
        new = local_log()[n0:]
        check("the secrets hook: exit 3 (vault locked) sends needs_you.vault_locked and keeps the exit code; others don't",
              rcs == [3, 0, 1] and [(e["family"], e["type"]) for e in new] == [("needs_you", "vault_locked")]
              and "secrets unlock" in new[0]["action"], (rcs, new))
        n1 = len(local_log())
        subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; cmd=secrets; {trap}\nexit 3'],
                       capture_output=True, text=True, env=menv, timeout=60, stdin=subprocess.DEVNULL)
        m_, s_ = pty.openpty()
        os.remove(os.path.join(events.state_dir(), "debounce.json"))
        # at the user's own terminal: no agent session running this test above it (tests/userterm.py)
        import userterm
        rt = userterm.spawn(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; cmd=secrets; {trap}\nexit 3'], env=menv,
                            stdin=s_, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(s_)
        rt.communicate(timeout=60)
        os.close(m_)
        check("L1 the secrets hook: once in 5 min, silent when you ran it at your terminal, worded neutrally",
              local_log()[n1:] == [] and rt.returncode == 3 and "isn't unlocked" in new[0]["message"]
              and "doesn't exist" in new[0]["message"], (local_log()[n1:], new[:1]))
        fake = userterm.fake_agent("codex")
        if fake:
            try:
                os.remove(os.path.join(events.state_dir(), "debounce.json"))
            except FileNotFoundError:
                pass
            n2 = len(local_log())
            m_, s_ = pty.openpty()
            ra = userterm.spawn([fake, "/bin/bash", "-c", f'. "{LAB}/scripts/lib.sh"; cmd=secrets; {trap}\nexit 3'],
                                env=menv, stdin=s_, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(s_)
            ra.communicate(timeout=60)
            os.close(m_)
            check("...but a terminal inside an agent session (Codex) isn't yours: the user hears about it",
                  ra.returncode == 3 and [(e["family"], e["type"]) for e in local_log()[n2:]] == [("needs_you", "vault_locked")],
                  local_log()[n2:])
        btrap = re.search(r"^trap 'rc=\$\?;.*?' EXIT$", open(os.path.join(LAB, "scripts", "build-vm.sh")).read(), re.M | re.S)
        rcs, n0 = [], len(local_log())
        for body in ("true", 'die "boom"', 'BUILD_QUIET_EXIT=1; die "exists, use --replace"', "exit 130"):
            rr = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; set -Eeuo pipefail; BUILD_QUIET_EXIT=0; '
                                 f'{btrap.group(0)}\n{body}'],
                                capture_output=True, text=True, env=menv, timeout=60, stdin=subprocess.DEVNULL)
            rcs.append(rr.returncode)
        new = local_log()[n0:]
        check("build-vm.sh's exit trap: done; failed (error); nothing for 'exists'; interrupted is info",
              btrap and rcs == [0, 1, 1, 130] and [(e["type"], e["severity"]) for e in new]
              == [("build_done", "info"), ("build_failed", "error"), ("build_failed", "info")], (rcs, new))
        r = subprocess.run([mc, "events", "watcher", "start"], capture_output=True, text=True, env=menv, timeout=60)
        pid = events.watcher_pid()
        check("events watcher start: a detached watcher, its pid in RUN_DIR", r.returncode == 0 and pid
              and "watcher running" in cli("status", "--line").stdout, r.stdout + r.stderr)
        r = subprocess.run([mc, "events", "watcher", "start"], capture_output=True, text=True, env=menv, timeout=60)
        check("starting it again keeps the one that runs", events.watcher_pid() == pid)
        check("nit: the watcher's log is 0600",
              oct(os.stat(os.path.join(ENV["MCOMP_LOG_DIR"], "events-watcher.log")).st_mode & 0o777) == "0o600")
        r = subprocess.run([mc, "events", "watcher", "stop"], capture_output=True, text=True, env=menv, timeout=60)
        check("events watcher stop", r.returncode == 0 and events.watcher_pid() is None
              and not os.path.exists(events.watcher_pidfile()), r.stdout + r.stderr)
    finally:
        pid = events.watcher_pid()
        if pid:
            os.kill(pid, 15)
        for rx in (web, otlp, ntfy):
            rx.close()
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
