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
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import ssl
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
       "MCOMP_VM_NAME": "testvm", "MCOMP_SSH_DIR": os.path.join(TD, "ssh")}
os.environ.update(ENV)
for k in ("MCOMP_ACTION_LOG", "MCOMP_CONFIG"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, os.path.join(LAB, "tests"))
import events  # noqa: E402

EV = os.path.join(LAB, "lib", "events.py")
PY = sys.executable
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)


def cli(*args, env=None, timeout=60):
    return subprocess.run([PY, EV, *args], capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, **(env or {})))


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
class HTTPRx:
    """Records every POST; `mode` = ok | 503 | 400."""

    def __init__(self):
        self.reqs, self.mode, rx = [], "ok", self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                code = {"ok": 200, "503": 503, "400": 400}[rx.mode]
                if code == 200:
                    rx.reqs.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
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
    """RFC 5425: TLS, then octet-counted RFC 5424 messages."""

    def __init__(self, cert, key):
        self.frames = []
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
            self.frames.append(buf[m.end():m.end() + n].decode())
            buf = buf[m.end() + n:]


def parse_5424(frame):
    m = re.match(r'<(\d+)>1 (\S+) (\S+) (\S+) (\S+) (\S+) (\[[^\]]*(?:\\\][^\]]*)*\]) (.*)$', frame, re.S)
    if not m:
        return None
    pri, ts, host, app, procid, msgid, sd, msg = m.groups()
    return {"pri": int(pri), "ts": ts, "host": host, "app": app, "procid": procid, "msgid": msgid, "sd": sd,
            "msg": json.loads(msg)}


# --- tests ----------------------------------------------------------------------------------
def main():
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
        r = cli("sink", "add", "web", "--kind", "webhook", "--url", web.url("/hook"))
        key = (re.search(r"^\s+([A-Za-z0-9+/=]{40,})$", r.stdout, re.M) or [None, None])[1]
        check("sink add webhook prints its signing key once", r.returncode == 0 and key, r.stdout + r.stderr)
        check("sink add otlp", cli("sink", "add", "collector", "--kind", "otlp", "--url", otlp.url()).returncode == 0)
        r = cli("sink", "add", "phone", "--kind", "ntfy", "--url", ntfy.url("/test-topic"), "--families", "needs_you",
                "--header", f"Authorization: Bearer {token}")
        check("sink add ntfy with a header", r.returncode == 0 and token not in r.stdout, r.stdout + r.stderr)
        if sysrx:
            r = cli("sink", "add", "siem", "--kind", "syslog", "--url", f"syslog+tls://localhost:{sysrx.port}",
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
        bad = [cli("sink", "add", "x1", "--kind", "ntfy", "--url", "http://example.com/t"),
               cli("sink", "add", "x2", "--kind", "webhook", "--url", "http://10.0.0.5/hook"),
               cli("sink", "add", "x3", "--kind", "syslog", "--url", "https://example.com:6514"),
               cli("sink", "add", "x4", "--kind", "webhook", "--url", "https://user:pw@example.com/h"),
               cli("sink", "add", "web", "--kind", "webhook", "--url", web.url())]
        check("refused: plain http off this Mac, syslog without TLS, credentials in the URL, a duplicate name",
              all(b.returncode == 2 for b in bad), [b.stderr for b in bad])
        os.environ["MCOMP_TEST_TOKEN"] = "abcdefghijkl0123"
        r = cli("sink", "add", "envhdr", "--kind", "webhook", "--url", web.url("/env"), "--header-env",
                "X-Api-Key=Key $MCOMP_TEST_TOKEN", "--families", "lifecycle", "--min-severity", "error")
        cfg = events.load_config()
        check("--header-env takes the value from the environment",
              r.returncode == 0 and cfg["sinks"]["envhdr"]["headers"] == [["X-Api-Key", "Key abcdefghijkl0123"]], r.stderr)
        cli("sink", "remove", "envhdr")

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
        check("the event schema", set(ce["data"]) == {"id", "seq", "time", "family", "type", "severity", "vm", "project",
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
        sec, err = HTTPRx(), HTTPRx()
        cli("sink", "add", "sec", "--kind", "webhook", "--url", sec.url(), "--families", "security")
        cli("sink", "add", "err", "--kind", "webhook", "--url", err.url(), "--min-severity", "error")
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
        cli("sink", "add", "act", "--kind", "webhook", "--url", act.url(), "--families", "actions")
        before = len(web.reqs)
        ev = events.emit("actions", "call", "click called", data={"tool": "click"})
        check("--families actions takes them; 'all' doesn't; the local log skips them",
              ev and len(act.reqs) == 1 and len(web.reqs) == before and len(local_log()) == n0, (len(act.reqs), len(web.reqs)))

        print("cleaning")
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
        check("every spool file is deleted, invalid and oversized ones included", os.listdir(sp) == [], os.listdir(sp))
        check("rejected files are counted", events.read_json(os.path.join(events.state_dir(), "spool.json"), {}).get("rejected") == 2)

        print("backlog and retry")
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
        cli("sink", "add", "hole", "--kind", "webhook", "--url", f"http://127.0.0.1:{hole.getsockname()[1]}/")
        cli("sink", "add", "dead", "--kind", "otlp", "--url", f"http://127.0.0.1:{dead_port}")
        t0 = time.time()
        ev = events.emit("task", "finished", "with a sink that never answers")
        dt = time.time() - t0
        check("a sink that never answers costs at most about 3 s", ev and dt < 4.5, dt)
        hs, ds = events.load_status("hole"), events.load_status("dead")
        check("timeouts and refused connections are recorded", hs["last_error"] == "timed out"
              and ds["last_error"] == "connection refused", (hs["last_error"], ds["last_error"]))
        t0 = time.time()
        events.emit("task", "finished", "again, during the backoff")
        check("during its backoff a failing sink isn't tried at all", time.time() - t0 < 1.5, time.time() - t0)
        r = cli("sink", "test", "dead")
        check("sink test reports a failure", r.returncode == 1 and "connection refused" in r.stdout, r.stdout)
        r = cli("sink", "test", "web")
        check("sink test reports success", r.returncode == 0 and r.stdout.startswith("OK"), r.stdout + r.stderr)
        for n in ("hole", "dead"):
            cli("sink", "remove", n)
        hole.close()
        check("sink remove deletes the sink and its backlog", "hole" not in events.load_config()["sinks"]
              and not os.path.exists(events.sink_file("hole", "queue")))

        print("order with several processes at once")
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
        d1 = events.emit("needs_you", "in_use", "busy", debounce="k1")
        d2 = events.emit("needs_you", "in_use", "busy", debounce="k1")
        d3 = events.emit("needs_you", "in_use", "busy", debounce="k2")
        time.sleep(0.6)
        d4 = events.emit("needs_you", "in_use", "busy", debounce="k1", debounce_s=0.5)
        check("an identical event within the window is dropped; another key or a later one isn't",
              d1 and d2 is None and d3 and d4, [bool(x) for x in (d1, d2, d3, d4)])

        print("notify")
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

        print("the bridge's hooks (no VM)")
        import mcp_lazy
        b = mcp_lazy.Bridge()
        sent = []
        b.send = sent.append
        b.busy_others = lambda: [{"kind": "session", "claude_pid": 4242, "cwd": "/example/other-project", "idle_s": 30,
                                  "last_tool": "click", "active": True}]
        n0 = len(local_log())
        b.on_tools_call({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "click", "arguments": {}}})
        b.on_tools_call({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "click", "arguments": {}}})
        events.flush_async(10)
        new = local_log()[n0:]
        check("VM in use by another controller: the call is refused and needs_you.in_use sent once (debounced)",
              sent and sent[0]["result"]["isError"] and [e["type"] for e in new] == ["in_use"]
              and new[0]["data"]["others"][0]["project"] == "/example/other-project", (sent[:1], new))
        b.busy_others = lambda: []
        b.connect = lambda: None
        b.send_up = lambda m: False
        n0 = len(local_log())
        b.on_tools_call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "set_value", "arguments": {"app": "Google Chrome", "value": "secret-typed"}}})
        events.flush_async(10)
        new = local_log()[n0:]
        check("the Chrome guard's refusal is a security event (without the value)",
              [e["type"] for e in new] == ["chrome_input_refused"] and "secret-typed" not in json.dumps(new), new)
        n0 = len(local_log())
        refusal = {"result": {"isError": True, "content": [{"type": "text", "text": "launch_app: outside the capability manifest"}]}}
        for i in range(3):
            b.tool_of[f"k{i}"] = "launch_app"
            b.observe(f"k{i}", json.loads(json.dumps(refusal)))
        events.flush_async(10)
        new = local_log()[n0:]
        check("a bounded-mode refusal tells the user only once it repeats, then debounces",
              [e["type"] for e in new] == ["refused"] and new[0]["data"]["count"] == 2, new)
        n0 = len(local_log())
        b.claim("click")
        b.claim("click")
        events.flush_async(10)
        new = local_log()[n0:]
        check("a controller taking over is a lifecycle event (once)", [e["type"] for e in new] == ["controller_connected"], new)

        print("the relay's actions events, actions verify, open-untrusted findings")
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

        print("CLI and the bash helper")
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
                                capture_output=True, text=True, env=menv, timeout=60)
            rcs.append(rr.returncode)
        new = local_log()[n0:]
        check("the secrets hook: exit 3 (vault locked) sends needs_you.vault_locked and keeps the exit code; others don't",
              rcs == [3, 0, 1] and [(e["family"], e["type"]) for e in new] == [("needs_you", "vault_locked")]
              and "secrets unlock" in new[0]["action"], (rcs, new))
        r = subprocess.run([mc, "events", "watcher", "start"], capture_output=True, text=True, env=menv, timeout=60)
        pid = events.watcher_pid()
        check("events watcher start: a detached watcher, its pid in RUN_DIR", r.returncode == 0 and pid
              and "watcher running" in cli("status", "--line").stdout, r.stdout + r.stderr)
        r = subprocess.run([mc, "events", "watcher", "start"], capture_output=True, text=True, env=menv, timeout=60)
        check("starting it again keeps the one that runs", events.watcher_pid() == pid)
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
