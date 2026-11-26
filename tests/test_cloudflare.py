#!/usr/bin/env python3
"""Cloudflare Containers (docs/cloudflare.md), against stand-ins for Cloudflare's REST API, the container registries and
the computer's control Worker. No VM, no network, no Cloudflare account.

    python3 tests/test_cloudflare.py

- The control Worker's own code (providers/cloudflare/worker/): tests/cloudflare_worker_test.mjs, run with node when
  it's there (its signature check, its host check, its egress plans, start, stop, checkpoints, the idle stop, a stop by
  Cloudflare, the refresh, forget; a note when node isn't there).
- The `cloudflare` transport (providers/cloudflare/adapter.py opener), the egress as policies, widening.
- wire.py: every request to the Worker signed (the key never sent, never in an error), workers.dev addresses only, the
  websocket's handshake (the Durable Object's id read before a byte goes through) and frames.
- api.py on a stand-in REST API: the upload (its bindings, its secret, its container and exports; sent once), the
  container application with Cloudflare's SSH off, errors scrubbed of the token and the account's ID.
- images.py: Ubuntu's image copied part by part, each checked by its digest; upload addresses elsewhere refused; Docker
  Hub's redirect followed without a credential; what Cloudflare won't delete said.
- The adapter: create (refused over a Worker of that name; each part journaled), every operation checked against the
  Worker's own word (this computer, its operation, its Durable Object) and the key's acceptance, stop, start,
  checkpoints, restore, egress read back, delete (never another's; residue with dates), status's notes, rates.
- lib/remote.py with the provider's hooks: the launch journaled before the Worker is uploaded, a lost upload found by its
  Worker's bindings (and one without them left alone), abort, wake, delete (journaled, a cut-off one finished, what stays
  at Cloudflare said and noted in the log folder, the key removed), plan, the transport's checks from the record alone.
- The keys: the computer's key 0600 beside its SSH keys, refused when it isn't private; given to the broker on its
  standard input only; the account's token in no child, no ProxyCommand, no argument.
- The broker, in this process with a stand-in Worker, and tunnel.py as a real program: the identity check, a
  connection carrying random bytes both ways, refused for another Durable Object, another computer or a stopped one
  (never started), quit, its end.
- The guest: the boot script's capability check (a process of an account but root's holding one ends them all), its
  command-line rules, sshd on port 2222, provision.sh's cloudflare target.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(LAB, "lib"))
import cloud  # noqa: E402
import remote  # noqa: E402
from cloud import capabilities as caps  # noqa: E402
from cloud import transport  # noqa: E402
from cloud.adapter import AdapterError  # noqa: E402

cf = cloud.registry.module("cloudflare")
capi = cloud.registry.module("cloudflare", "api")
images = cloud.registry.module("cloudflare", "images")
wire = cloud.registry.module("cloudflare", "wire")
hooks = cloud.registry.module("cloudflare", "remote")
bk = cloud.registry.module("cloudflare", "broker")
GUEST = os.path.join(LAB, "providers", "cloudflare", "guest")
BOOT = os.path.join(GUEST, "rootfs", "opt", "metacomputer", "cloudflare", "boot")
TUNNEL = os.path.join(LAB, "providers", "cloudflare", "tunnel.py")

FAILS = []
# Stand-in credentials, built from a description (no key-like literal in this file): the token a run of one letter, the
# account's ID 32 hex digits of a pattern, the computer's key 64 of another.
TOKEN = "t" * 40
ACCOUNT = "ab" * 16
CREDS = {"CLOUDFLARE_API_TOKEN": TOKEN, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
OP = "0123456789abcdef0123456789abcdef"
SUB = "acct-sub"
# A stand-in image: its manifest's bytes and their digest (the pin, swapped in for the tests).
CONFIG = b'{"architecture":"amd64","os":"linux"}'
LAYER = b"layer-bytes" * 100
FAKE_MANIFEST = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "size": len(CONFIG),
                                       "digest": "sha256:" + hashlib.sha256(CONFIG).hexdigest()},
                            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": len(LAYER),
                                        "digest": "sha256:" + hashlib.sha256(LAYER).hexdigest()}]}).encode()
FAKE_DIGEST = "sha256:" + hashlib.sha256(FAKE_MANIFEST).hexdigest()


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn, *a, kind=Exception, **k):
    try:
        fn(*a, **k)
    except kind as e:
        return str(e) or type(e).__name__
    except Exception as e:      # noqa: BLE001
        return f"WRONG {type(e).__name__}: {e}"
    return None


def do_id(ns):
    return hashlib.sha256(f"{ns}:computer".encode()).hexdigest()


# --- the stand-in: Cloudflare in memory ---------------------------------------------------------------------------
class FakeDO:
    """A computer's control Worker and its Durable Object, as the Mac sees them: each request's signature checked with
    the Worker's secret, then the verbs, in memory."""

    def __init__(self, cf_, name):
        self.cf, self.name = cf_, name
        self.rec = {"state": "never", "running": False, "latest": None, "snaps": {}, "checkpoints": {}, "internet": True,
                    "allow": [], "setup_done": False, "idle_min": 15, "lost": None, "force": None, "dropped": []}
        self.n = 0
        self.execs = []
        self.exec_answer = lambda argv, stdin: (0, b"", b"")
        self.answer_as = None          # {"computer"/"op"/"do": ...} overrides in status, to play another computer
        self.fail_destroy = 0          # stops whose container Cloudflare doesn't end (the Worker's "stopping")
        self.refresh = []              # the Worker's refresh trouble (logic.js refreshTrouble), as status says it

    def script(self):
        return self.cf.scripts[self.name]

    def plain(self, k):
        return next((b.get("text") for b in self.script()["bindings"] if b["name"] == k), None)

    def snap(self):
        self.n += 1
        sid = f"snap-{self.name}-{self.n}"
        now = int(time.time() * 1000)
        self.rec["snaps"][sid] = {"id": sid, "size": 1_200_000_000, "made": now, "fresh": now,
                                  "expires": now + 30 * 86400000}
        return sid

    def status(self):
        r = self.rec
        out = {"computer": self.plain("MCOMP_COMPUTER"), "op": self.plain("MCOMP_OP"), "do": do_id(self.cf.namespaces[self.name]),
               "running": r["running"], "state": r["state"], "size": self.plain("MCOMP_SIZE"), "idle_min": r["idle_min"],
               "setup_done": r["setup_done"],
               "egress": {"setting": "internet" if r["internet"] else ("allowlist" if r["allow"] else "off"),
                          "allow": r["allow"], "in_force": r["force"] if r["running"] else None},
               "latest": r["snaps"].get(r["latest"]), "checkpoints": {k: r["snaps"].get(v) for k, v in r["checkpoints"].items()},
               "lost": r["lost"], "residue": [dict(v, labels=[]) for v in r["snaps"].values()] + r["dropped"],
               "stop_pending": r.get("stop_pending"), "refresh": self.refresh, "now": int(time.time() * 1000)}
        out.update(self.answer_as or {})
        return out

    def handle(self, verb, b):
        r = self.rec
        if verb == "status":
            return 200, self.status()
        if verb == "start":
            if r["running"]:
                return 200, {"already": True}
            if not r["latest"] and not b.get("from_image"):
                return 409, {"error": "it has no snapshot to start from"}
            r.update(running=True, state="running", lost=None,
                     force={"internet": r["internet"], "names": ({} if r["internet"] else
                                                                  {"metacomputer.invalid": "deny", **{n: "allow" for n in r["allow"]}})})
            if b.get("idle_min"):
                r["idle_min"] = b["idle_min"]
            return 200, {"started_ms": 200, "booted_ms": 3000 if b.get("boot") else None}
        if verb == "stop":
            if not r["running"]:
                return 200, {"already": True}
            if r["state"] != "stopping":
                r["latest"] = self.snap()
            if self.fail_destroy > 0:
                self.fail_destroy -= 1
                tries = (r.get("stop_pending") or {}).get("tries", 0) + 1
                r.update(state="stopping", stop_pending={"why": "stop", "tries": tries, "error": "internal error"})
                return 503, {"error": f"its disk was saved (snapshot {r['latest']}), but Cloudflare didn't end the "
                                      "container (internal error): it still runs, and its Worker tries to end it again "
                                      "within a minute; nothing is lost"}
            r.update(running=False, state="stopped", force=None, stop_pending=None)
            return 200, {"snapshot": r["snaps"][r["latest"]], "ms": 20000}
        if verb == "snapshot":
            if not r["running"]:
                return 409, {"error": "it's stopped"}
            if b["label"] in r["checkpoints"] and not b.get("replace"):
                return 409, {"error": "exists"}
            sid = self.snap()
            old = r["checkpoints"].get(b["label"])
            r["checkpoints"][b["label"]] = sid
            return 200, {"label": b["label"], "snapshot": r["snaps"][sid], "ms": 19000,
                         "let_go": {"id": old, "expires": 1} if old else None}
        if verb == "restore":
            sid = r["checkpoints"].get(b["label"])
            if not sid:
                return 404, {"error": "no checkpoint"}
            r.update(latest=sid, running=True, state="running")
            return 200, {"ms": 4000}
        if verb == "uncheckpoint":
            sid = r["checkpoints"].pop(b["label"], None)
            return (404, {"error": "no checkpoint"}) if not sid else (200, {"let_go": {"id": sid, "expires": 1800000000000}})
        if verb == "net":
            if b.get("allow") is not None:
                r.update(internet=False, allow=list(b["allow"]))
            else:
                r.update(internet=b["egress"] == "internet", allow=[])
            restarted = False
            if r["running"]:
                restarted = r["force"]["internet"] != r["internet"]
                r["force"] = {"internet": r["internet"], "names": ({} if r["internet"] else
                                                                   {"metacomputer.invalid": "deny", **{n: "allow" for n in r["allow"]}})}
            return 200, {"restarted": restarted}
        if verb == "exec":
            if r["setup_done"]:
                return 403, {"error": "this computer is set up: commands through the Worker are closed"}
            stdin = base64.b64decode(b["stdin_b64"]) if b.get("stdin_b64") else None
            self.execs.append((b["argv"], stdin))
            code, so, se = self.exec_answer(b["argv"], stdin)
            return 200, {"code": code, "stdout_b64": base64.b64encode(so).decode(), "stderr_b64": base64.b64encode(se).decode()}
        if verb == "boot":
            self.execs.append((["BOOT", "restart-desktop" if b.get("restart_desktop") else ""], None))
            return 200, {"booted_ms": 3000}
        if verb == "logs":
            return 200, {"code": 0, "stdout_b64": base64.b64encode(b"log line\n").decode()}
        if verb == "setup-done":
            r["setup_done"] = True
            return 200, {"setup_done": True}
        if verb == "settings":
            r["idle_min"] = b.get("idle_min", r["idle_min"])
            return 200, {}
        if verb == "forget":
            res = [dict(v, labels=sorted(k for k, sid in r["checkpoints"].items() if sid == v["id"])) for v in r["snaps"].values()]
            self.rec = {"forgotten": True}
            return 200, {"residue": res}
        return 404, {"error": "no such request"}


class CF:
    """Cloudflare in memory: its REST API (provider_http send's signature), the registries and each Worker's
    workers.dev address (wire.https's signature). Every call logged."""

    def __init__(self):
        self.scripts, self.namespaces, self.apps, self.dos = {}, {}, {}, {}
        self.log, self.reg_log = [], []
        self.prepare = ["pending", "ready"]
        self.lose_upload = False
        self.hidden = 0
        self.ssh_on_create = False
        self.fail_delete_app = 0
        self.repos = {}                       # repo -> {ref: (ctype, bytes)}
        self.blobs = set()
        self.pw = "p" * 30
        self.n = 0
        self.upload_location_host = None
        self.nonces = set()
        self.on_prepare = None                # called at each image-preparation poll (another create racing this one)

    # REST
    def http(self, method, url, headers, body=None, timeout=60):
        assert url.startswith("https://api.cloudflare.com/client/v4/"), url
        assert headers.get("Authorization") == f"Bearer {TOKEN}"
        path, _, qs = url[len("https://api.cloudflare.com/client/v4"):].partition("?")
        self.log.append((method, path, qs, body, dict(headers)))
        pre = f"/accounts/{ACCOUNT}"
        assert path.startswith(pre), path
        p = path[len(pre):]
        st, obj = self.route(method, p, qs, body, headers)
        return st, {}, json.dumps(obj).encode()

    def ok(self, result):
        return 200, {"success": True, "errors": [], "result": result}

    def err(self, status, code, message):
        return status, {"success": False, "errors": [{"code": code, "message": message}], "result": None}

    def route(self, method, p, qs, body, headers):
        m = re.fullmatch(r"/workers/scripts/([a-z0-9-]+)(/settings|/subdomain)?", p)
        if p == "/workers/subdomain":
            return self.ok({"subdomain": SUB})
        if p == "/workers/scripts" and method == "GET":
            return self.ok([{"id": n} for n in self.scripts])
        if m and m.group(2) == "/settings":
            if m.group(1) not in self.scripts or self.hidden > 0:
                self.hidden = max(0, self.hidden - 1)
                return self.err(404, 10007, "This Worker can't be found")
            return self.ok({"bindings": [b for b in self.scripts[m.group(1)]["bindings"] if b["type"] != "secret_text"]
                            + [{"name": "MCOMP_KEY", "type": "secret_text"}]})
        if m and m.group(2) == "/subdomain" and method == "POST":
            self.scripts[m.group(1)]["workers_dev"] = json.loads(body)
            return self.ok({"enabled": True})
        if m and not m.group(2) and method == "PUT":
            name = m.group(1)
            ctype = headers.get("Content-Type", "")
            meta, mods = parse_multipart(ctype, body)
            self.n += 1
            ns = hashlib.md5(f"ns{self.n}{name}".encode()).hexdigest()
            self.scripts[name] = {"bindings": meta["bindings"], "meta": meta, "modules": mods}
            self.namespaces[name] = ns
            self.dos[name] = FakeDO(self, name)
            if self.lose_upload:
                raise OSError("connection reset by peer")
            return self.ok({"id": name, "startup_time_ms": 2})
        if m and not m.group(2) and method == "DELETE":
            if m.group(1) not in self.scripts:
                return self.err(404, 10007, "not found")
            del self.scripts[m.group(1)]
            self.namespaces.pop(m.group(1), None)
            return self.ok(None)
        if p == "/workers/durable_objects/namespaces":
            return self.ok([{"id": ns, "script": s, "class": "Computer", "name": f"{s}_Computer"} for s, ns in self.namespaces.items()])
        if p == "/containers/image-preparations":
            if self.on_prepare:
                self.on_prepare(self)
            return self.ok({"status": self.prepare.pop(0) if len(self.prepare) > 1 else self.prepare[0]})
        m2 = re.fullmatch(r"/containers/applications(?:/([0-9a-f]{32}))?", p)
        if m2 and method == "POST":
            b = json.loads(body)
            cfg = b.get("configuration") or {}
            if self.ssh_on_create:
                cfg = {"wrangler_ssh": {"enabled": True}}
            ns = b["durable_objects"]["namespace_id"]
            self.apps[ns] = {"id": ns, "name": b["name"], "scheduling_policy": b["scheduling_policy"], "configuration": cfg}
            return self.ok(self.apps[ns])
        if m2 and m2.group(1) and method == "GET":
            return self.ok(self.apps[m2.group(1)]) if m2.group(1) in self.apps else self.err(404, "NOT_FOUND", "no app")
        if m2 and m2.group(1) and method == "PATCH":
            self.apps[m2.group(1)]["configuration"] = json.loads(body)["configuration"]
            return self.ok(self.apps[m2.group(1)])
        if m2 and m2.group(1) and method == "DELETE":
            if self.fail_delete_app > 0:
                self.fail_delete_app -= 1
                return self.err(500, "INTERNAL", "try again")
            return self.ok(self.apps.pop(m2.group(1))) if m2.group(1) in self.apps else self.err(404, "NOT_FOUND", "no app")
        if m2 and method == "GET":
            return self.ok(list(self.apps.values()))
        if p == "/containers/registries/registry.cloudflare.com/credentials":
            return self.ok({"username": "v1", "password": self.pw})
        if re.fullmatch(r"/containers/instances/[0-9a-f]{64}/ssh", p):
            return self.err(403, "SSH_NOT_ALLOWED", "")
        return self.err(404, 7000, f"no route {method} {p}")

    # registries and workers.dev
    def send(self, host, method, path, headers, body=None, timeout=60, cap=1 << 24):
        self.reg_log.append((host, method, path, dict(headers)))
        if host.endswith(f".{SUB}.workers.dev"):
            worker = host.split(".")[0]
            if worker not in self.scripts:
                return 404, {}, b"<html>nothing here</html>"
            key = next(b["text"] for b in self.scripts[worker]["bindings"] if b["name"] == "MCOMP_KEY")
            auth = headers.get("X-Mc-Auth", "")
            if not verify_sig(key, method, path, auth, body or b"") or auth.split(".")[2] in self.nonces:
                return 401, {}, b'{"error":"unauthorized"}'
            self.nonces.add(auth.split(".")[2])
            verb = path[len("/v1/"):]
            st, obj = self.dos[worker].handle(verb, json.loads(body or b"{}"))
            return st, {}, json.dumps(obj).encode()
        if host == "auth.docker.io":
            return 200, {}, json.dumps({"token": "hub-token"}).encode()
        if host == "registry-1.docker.io":
            assert headers.get("Authorization") == "Bearer hub-token"
            if "/manifests/" in path:
                return 200, {"content-type": "application/vnd.oci.image.manifest.v1+json"}, FAKE_MANIFEST
            d = path.rsplit("/", 1)[1]
            return 307, {"location": f"https://cdn.example/blob/{d}?sig=x"}, b""
        if host == "cdn.example":
            assert "Authorization" not in headers, "a credential went to Docker Hub's download address"
            d = path.split("/")[2].split("?")[0]
            data = {"sha256:" + hashlib.sha256(CONFIG).hexdigest(): CONFIG, "sha256:" + hashlib.sha256(LAYER).hexdigest(): LAYER}[d]
            return 200, {}, data if not getattr(self, "tamper", False) else data + b"!"
        if host == "registry.cloudflare.com":
            want = "Basic " + base64.b64encode(f"v1:{self.pw}".encode()).decode()
            if headers.get("Authorization") != want:
                return 401, {}, b"{}"
            m = re.fullmatch(rf"/v2/{ACCOUNT}/([a-z0-9.-]+)/(manifests|blobs|tags)/?(.*)", path.split("?")[0])
            if path == "/v2/gc/layers":
                return 200, {}, b'{"deleted":[]}'
            if not m:
                if re.fullmatch(rf"/v2/{ACCOUNT}/[a-z0-9.-]+/blobs/uploads/u1", path.split("?")[0]) and method == "PUT":
                    d = path.split("digest=")[1]
                    assert hashlib.sha256(body).hexdigest() == d.split(":")[1]
                    self.blobs.add(d)
                    return 201, {}, b""
                return 404, {}, b"{}"
            repo, kind, rest = m.groups()
            r = self.repos.setdefault(repo, {})
            if kind == "blobs" and rest == "uploads/" and method == "POST":
                loc = f"/v2/{ACCOUNT}/{repo}/blobs/uploads/u1"
                if self.upload_location_host:
                    loc = f"https://{self.upload_location_host}{loc}"
                return 202, {"location": loc}, b""
            if kind == "blobs" and rest == "uploads/u1" and method == "PUT":
                d = path.split("digest=")[1]
                assert hashlib.sha256(body).hexdigest() == d.split(":")[1]
                self.blobs.add(d)
                return 201, {}, b""
            if kind == "blobs" and method == "HEAD":
                return (200 if rest in self.blobs else 404), {}, b""
            if kind == "manifests":
                if method == "HEAD":
                    return (200 if rest in r else 404), {}, b""
                if method == "PUT":
                    r[rest] = (headers.get("Content-Type"), body)
                    return 201, {"docker-content-digest": "sha256:" + hashlib.sha256(body).hexdigest()}, b""
                if method == "GET":
                    return (200, {}, r[rest][1]) if rest in r else (404, {}, b"{}")
                if method == "DELETE":
                    if rest not in r:
                        return 404, {}, b"{}"
                    if rest.startswith("rootfs-"):
                        return 403, {}, b'{"errors":[{"code":"DENIED","message":"snapshot manifests cannot be deleted"}]}'
                    if rest.startswith("conv-"):
                        return 409, {}, b'{"code":"SINGLETON_ARTIFACT_EXISTS"}'
                    del r[rest]
                    return 204, {}, b""
            if kind == "tags":
                return 200, {}, json.dumps({"tags": sorted(r)}).encode()
        raise AssertionError(f"a request to {host}{path}")


def verify_sig(key, method, path, header, body):
    """The Worker's check, written again here (not imported): HMAC-SHA256 of v1, method, path, query, time, nonce, body
    hash."""
    m = re.fullmatch(r"v1\.([0-9]{9,12})\.([0-9a-f]{24})\.([0-9a-f]{64})", header or "")
    if not m or abs(int(time.time()) - int(m.group(1))) > 300:
        return False
    p, _, q = path.partition("?")
    data = "\n".join(["v1", method, p, q, m.group(1), m.group(2), hashlib.sha256(body).hexdigest()]).encode()
    return hmac.compare_digest(hmac.new(key.encode(), data, hashlib.sha256).hexdigest(), m.group(3))


def parse_multipart(ctype, body):
    boundary = ctype.split("boundary=")[1]
    meta, mods = None, {}
    for part in body.split(f"--{boundary}".encode())[1:-1]:
        head, _, data = part.strip(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        if name == "metadata":
            meta = json.loads(data)
        else:
            assert b"application/javascript+module" in head
            mods[name] = data
    return meta, mods


def rest_on(c):
    return capi.API(env=dict(CREDS), http=c.http, sleep=lambda s: None)


def computer(name="box", **extra):
    return dict({"VM_NAME": name, "PROVIDER": "cloudflare", "PLACE": "cloudflare", "TRANSPORT": "cloudflare",
                 "EGRESS": "internet", "SIZE": "standard-3", "IDLE_STOP": "15"}, **extra)


# --- the Worker's own code -----------------------------------------------------------------------------------------
def worker_js():
    print("the control Worker's own code (node)")
    node = shutil.which("node")
    if not node:
        print("NOTE node isn't on this Mac: tests/cloudflare_worker_test.mjs (the Worker's own checks) was skipped")
        return
    v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"v(\d+)\..*", v) or int(re.match(r"v(\d+)", v).group(1)) < 20:
        print(f"NOTE node {v} is older than 20: the Worker's own checks were skipped")
        return
    r = subprocess.run([node, os.path.join(TESTS, "cloudflare_worker_test.mjs")], capture_output=True, text=True, timeout=300)
    lines = r.stdout.splitlines()
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    passes = [ln for ln in lines if ln.startswith("PASS")]
    for ln in fails:
        print("  " + ln)
    check(f"the Worker's checks under node {v}: all of them pass ({len(passes)} passed)", r.returncode == 0 and not fails
          and len(passes) >= 90, (r.returncode, fails[:3], r.stderr[-400:]))


# --- the transport and the egress ----------------------------------------------------------------------------------
def transport_and_policy():
    print("\nthe cloudflare transport and the egress")
    s = computer("box", CLOUDFLARE_WORKER="mc-box")
    ep = transport.endpoint(s)
    d, sock = cf.broker_paths(LAB, "box")
    check("the cloudflare transport: tunnel.py relay to the broker's socket as the ProxyCommand, pinned as mcomp-NAME",
          ep["host"] == "mc-box" and ep["alias"] == "mcomp-box" and ep["transport"] == "cloudflare"
          and ep["proxy"] == [TUNNEL, "relay", "--sock", sock, "--worker", "mc-box"], ep)
    check("...the socket in this account's own folder under /tmp, short enough for a Unix socket",
          d == f"/tmp/mcomp-{os.getuid()}-cloudflare" and len(sock) < 100 and sock.startswith(d + "/"), sock)
    check("...the Worker's name defaults to metacomputer-NAME", transport.endpoint(computer("Box_1.x"))["host"] == "metacomputer-box-1-x")
    for bad in ("Mc", "-x", "a b", "x" * 60, "a_b"):
        check(f"...a Worker's name {bad!r} is refused", raises(transport.endpoint, computer("box", CLOUDFLARE_WORKER=bad)) is not None)
    check("instance ids: the Worker's name and 16 hex digits of its Durable Object's id; one that isn't refused",
          cf.instance_id("mc-box", "f" * 64) == "mc-box-" + "f" * 16 and cf.parse_instance("mc-box-" + "f" * 16) == ("mc-box", "f" * 16)
          and raises(cf.instance_id, "mc-box", "x") is not None and cf.parse_instance("mc-box") == (None, None))
    check("...the identity fits the vault's tunnel id ([A-Za-z0-9_-], 80 at most)",
          re.fullmatch(r"[A-Za-z0-9_-]{4,80}", cf.instance_id("a" * 54, "f" * 64)) is not None)
    i, o = cf.policy_for("internet"), cf.policy_for("off")
    a = cf.policy_for(allow=["Example.COM.", "b.org", "example.com"])
    check("egress as policies: internet, off, an allowlist of exact names (lowercased, no duplicate)",
          i == {"internet": True, "allow": []} and o == {"internet": False, "allow": []}
          and a == {"internet": False, "allow": ["example.com", "b.org"]}, a)
    for bad in ("*.example.com", "10.0.0.1", "localhost", "exa mple.com", "metacomputer.invalid"):
        check(f"...{bad!r} can't be allowed (wildcards, addresses, the sentinel)", raises(cf.policy_for, allow=[bad]) is not None)
    check("...64 names: refused (the sentinel takes one of Cloudflare's 64)", raises(cf.policy_for, allow=[f"n{i}.com" for i in range(64)]) is not None)
    check("read back: what's in force ({internet, names}) and what's set ({internet, allow})",
          cf.policy_kind({"internet": True, "names": {}}) == ("internet", [])
          and cf.policy_kind({"internet": False, "names": {"metacomputer.invalid": "deny"}}) == ("off", [])
          and cf.policy_kind({"internet": False, "names": {"metacomputer.invalid": "deny", "a.org": "allow", "b.org": "deny"}}) == ("allowlist", ["a.org"])
          and cf.policy_kind({"internet": True, "names": {"a.org": "allow"}})[0] == "other"
          and cf.policy_kind({"internet": False, "names": {"a.org": "maybe"}})[0] == "other"
          and cf.policy_kind(a) == ("allowlist", ["example.com", "b.org"]) and cf.policy_kind(None)[0] == "other")
    check("widening: off -> internet widens; internet -> a list doesn't; a list -> a longer one does; anything -> off "
          "doesn't; 'other' in force -> anything but off does",
          cf.widens("off", [], i) and not cf.widens("internet", [], a) and cf.widens("allowlist", ["b.org"], a)
          and not cf.widens("allowlist", ["b.org"], o) and cf.widens("other", [], cf.policy_for(allow=["x.com"]))
          and not cf.widens("other", [], o) and not cf.widens("allowlist", ["b.org", "example.com"], a))


def manifest():
    print("\nthe manifest")
    m = cf.MANIFEST
    check("sound", caps.check_manifest(m) == [], caps.check_manifest(m))
    check("stop-start, no program kept; snapshots; foreground input only; billing storage while stopped; a beta",
          m["sleep_wake"] == {"kind": "stop-start", "keeps_processes": False} and m["snapshots"]
          and m["input"]["background"] is False and m["billing"]["stopped"] == "storage" and m["beta"] is True)
    check("...its billing note says the snapshots are free during Cloudflare's beta, honestly",
          "free during Cloudflare's beta" in m["billing"]["note"])
    check("...egress by outbound interception (an allowlist), private ranges and metadata refused outside, off possible",
          m["egress"] == {"model": "http-intercept", "allowlist": True, "private_ranges_blocked": True}
          and m["outside_egress"] == {"enforced": True, "private_ranges": True, "metadata": True, "off": True})
    check("...sizes within 4 vCPU, 12 GiB, 20 GB; no regions", m["sizes"] == ("standard-2", "standard-3", "standard-4")
          and max(v[0] for v in cf.SIZES.values()) == 4 and max(v[1] for v in cf.SIZES.values()) == 12
          and max(v[2] for v in cf.SIZES.values()) == 20 and m["regions"] == ())
    c = cloud.registry.manifest("cloudflare").CLI
    check("CLI: the account's token and ID its only credentials; its transport, its --worker option; owners @jonradoff",
          c["credentials"] == ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID") and c["transport"] == "cloudflare"
          and c["create_options"][0][0] == "--worker" and c["owners"] == ("@jonradoff",) and c["api_host"] == "api.cloudflare.com")
    check("...the image pinned by its manifest's digest", re.fullmatch(r"sha256:[0-9a-f]{64}", cloud.registry.manifest("cloudflare").IMAGE_DIGEST) is not None)
    a = cf.Cloudflare()
    r = a.rates("standard-3")
    check("rates: standard-3 about $0.08/h driven now and then, nothing while stopped (the snapshots free in the beta)",
          0.07 < r["awake_per_hour"] < 0.09 and r["asleep_per_hour"] == 0.0 and "free during" in r["note"], r)
    check("sizes and IDLE_STOP checked: another size, a region, IDLE_STOP off or past 350 refused",
          raises(a.size_of, "standard-1") and raises(a.region_of, "iad") and raises(a.idle_of, "off")
          and raises(a.idle_of, "351") and a.idle_of("350") == 350 and a.idle_of(None) == 15)


# --- wire.py -----------------------------------------------------------------------------------------------------------
def wire_tests():
    print("\nwire.py: the Worker's requests and websocket")
    key = "c" * 64
    sent = []

    def send(host, method, path, headers, body=None, timeout=60):
        sent.append((host, method, path, dict(headers), body))
        return 200, {}, b'{"ok": true}'
    w = wire.Worker(f"https://mc-box.{SUB}.workers.dev", key, send=send)
    w.call("status", {"a": 1})
    host, method, path, headers, body = sent[-1]
    check("a request: POST /v1/VERB to the Worker's workers.dev name, signed (X-Mc-Auth), the key itself nowhere in it",
          host == f"mc-box.{SUB}.workers.dev" and method == "POST" and path == "/v1/status"
          and verify_sig(key, "POST", path, headers["X-Mc-Auth"], body)
          and key not in json.dumps(headers) and key.encode() not in body, headers)
    check("...signed with another key it wouldn't pass", not verify_sig("d" * 64, "POST", path, headers["X-Mc-Auth"], body))
    for bad in ("http://mc-box.x.workers.dev", "https://evil.example.com", "https://a.b.c.workers.dev",
                "https://mc-box.x.workers.dev.evil.com", "https://mc-box.x.workers.dev/path"):
        check(f"...an address that isn't a Worker's workers.dev name: refused ({bad})", raises(wire.Worker, bad, key) is not None)
    check("...a key that isn't one metacomputer made: refused", raises(wire.Worker, f"https://mc-box.{SUB}.workers.dev", "short") is not None)

    def refusing(host, method, path, headers, body=None, timeout=60):
        return 401, {}, json.dumps({"error": f"unauthorized {key} {ACCOUNT}"}).encode()
    w2 = wire.Worker(f"https://mc-box.{SUB}.workers.dev", key, send=refusing)
    e = raises(w2.call, "status")
    check("a refusal: WorkerError with its status; the key and anything shaped like an account's ID scrubbed",
          e and key not in e and ACCOUNT not in e and "unauthorized" in e, e)
    check("...a verb that isn't one: refused before anything is sent", raises(w.call, "../x") is not None and len(sent) == 1)
    # the websocket: a stand-in server on a socketpair
    srv, cli = socket.socketpair()
    do = "e" * 64

    def server():
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += srv.recv(4096)
        head = buf.decode()
        k = re.search(r"Sec-WebSocket-Key: (\S+)", head).group(1)
        auth = re.search(r"X-Mc-Auth: (\S+)", head).group(1)
        ok = verify_sig(key, "GET", "/v1/tcp", auth, b"")
        acc = base64.b64encode(hashlib.sha1((k + wire.WS_GUID).encode()).digest()).decode()
        srv.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {acc}\r\nx-mc-do: {do}\r\nx-ok: {ok}\r\n\r\n").encode())
        # as the server: each message back, unmasked
        while True:
            got = _server_recv(srv)
            if got is None:
                break
            _server_send(srv, got)
    t = threading.Thread(target=server, daemon=True)
    t.start()
    w3 = wire.Worker(f"https://mc-box.{SUB}.workers.dev", key, connect=lambda h, tmo: cli)
    ws, got_do = w3.tcp()
    data = os.urandom(200_000)
    threading.Thread(target=ws.send, args=(data,), daemon=True).start()     # (the echo writes back meanwhile)
    back = bytearray()
    while len(back) < len(data):
        m = ws.recv()
        if m is None:
            break
        back += m
    check("the websocket: signed, its Durable Object's id read from the 101 before a byte goes through, bytes both ways",
          got_do == do and bytes(back) == data, (got_do, len(back)))
    ws.close()
    check("mask: a frame's payload masked and unmasked to itself", wire.mask(wire.mask(b"hello world!", b"abcd"), b"abcd") == b"hello world!")
    # a 401 instead of a 101
    a1, b1 = socket.socketpair()

    def refuse():
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += a1.recv(4096)
        a1.sendall(b"HTTP/1.1 401 Unauthorized\r\ncontent-type: application/json\r\n\r\n{\"error\":\"unauthorized\"}")
        a1.close()
    threading.Thread(target=refuse, daemon=True).start()
    e = raises(wire.Worker(f"https://mc-box.{SUB}.workers.dev", key, connect=lambda h, tmo: b1).tcp)
    check("...refused (401): WorkerError, no websocket", e and "unauthorized" in e, e)
    a2, b2 = socket.socketpair()

    def no_do():
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += a2.recv(4096)
        k = re.search(r"Sec-WebSocket-Key: (\S+)", buf.decode()).group(1)
        acc = base64.b64encode(hashlib.sha1((k + wire.WS_GUID).encode()).digest()).decode()
        a2.sendall(f"HTTP/1.1 101 OK\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
    threading.Thread(target=no_do, daemon=True).start()
    e = raises(wire.Worker(f"https://mc-box.{SUB}.workers.dev", key, connect=lambda h, tmo: b2).tcp)
    check("...a 101 that doesn't say which Durable Object it reached: refused", e and "didn't say" in e, e)


def _server_recv(s):
    def need(n):
        out = b""
        while len(out) < n:
            d = s.recv(n - len(out))
            if not d:
                raise EOFError
            out += d
        return out
    try:
        b0, b1 = need(2)
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(need(2), "big")
        elif n == 127:
            n = int.from_bytes(need(8), "big")
        k = need(4) if b1 & 0x80 else None
        data = need(n)
        if (b0 & 0x0F) == 0x8:
            return None
        return wire.mask(data, k) if k else data
    except (EOFError, OSError):
        return None


def _server_send(s, data):
    n = len(data)
    head = bytes([0x82]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big") if n < 65536
                            else bytes([127]) + n.to_bytes(8, "big"))
    s.sendall(head + data)


# --- api.py and images.py --------------------------------------------------------------------------------------------
def api_and_images():
    print("\napi.py and images.py on the stand-ins")
    c = CF()
    rest = rest_on(c)
    rest.upload("mc-box", {"index.js": b"export default {}", "logic.js": b"export const x = 1"},
                {"MCOMP_COMPUTER": "box", "MCOMP_OP": OP, "MCOMP_SIZE": "standard-3"}, "c" * 64,
                rest.image_ref("mc-box", FAKE_DIGEST), "standard-3")
    meta = c.scripts["mc-box"]["meta"]
    put = [x for x in c.log if x[0] == "PUT"][-1]
    check("upload: one PUT of the script (excludeScript), its two modules", put[1].endswith("/workers/scripts/mc-box")
          and "excludeScript=true" in put[2] and set(c.scripts["mc-box"]["modules"]) == {"index.js", "logic.js"})
    check("...its bindings: the computer, its operation and size as plain text, the key as its one secret, the Durable "
          "Object class", {b["name"]: b["type"] for b in meta["bindings"]} == {"MCOMP_COMPUTER": "plain_text",
                                                                                "MCOMP_OP": "plain_text", "MCOMP_SIZE": "plain_text",
                                                                                "MCOMP_KEY": "secret_text", "COMPUTER": "durable_object_namespace"})
    check("...its container: the Durable Object class Computer, its image by digest; exported on SQLite storage",
          meta["containers"] == [{"name": "mc-box", "class_name": "Computer", "images": {"ubuntu": f"registry.cloudflare.com/{ACCOUNT}/mc-box@{FAKE_DIGEST}"}}]
          and meta["exports"] == {"Computer": {"type": "durable-object", "storage": "sqlite", "container": "mc-box"}}
          and meta["observability"] == {"enabled": False} and meta["main_module"] == "index.js")
    c.lose_upload = True
    n = len([x for x in c.log if x[0] == "PUT"])
    e = raises(rest.upload, "mc-two", {"index.js": b"x"}, {}, "c" * 64, rest.image_ref("mc-two", FAKE_DIGEST), "standard-3")
    c.lose_upload = False
    check("...an upload whose answer is lost: sent once (never again), said as a launch that may have happened",
          e and "didn't answer" in e and len([x for x in c.log if x[0] == "PUT"]) == n + 1, e)
    rest.enable_workers_dev("mc-box")
    check("workers.dev turned on, previews off (nothing else: no route)", c.scripts["mc-box"]["workers_dev"] == {"enabled": True, "previews_enabled": False}
          and not any("routes" in x[1] or "/zones" in x[1] for x in c.log))
    ns = rest.namespace_of("mc-box")
    app = rest.create_application("mc-box", ns)
    body = json.loads([x for x in c.log if x[0] == "POST" and x[1].endswith("/containers/applications")][-1][3])
    check("the container application: of the Worker's namespace, the durable_object policy, Cloudflare's SSH off",
          body == {"name": "mc-box", "scheduling_policy": "durable_object", "durable_objects": {"namespace_id": ns},
                   "configuration": {"wrangler_ssh": {"enabled": False}}} and app["id"] == ns, body)
    check("the instance's SSH endpoint read as a refusal (403), never used", rest.instance_ssh("f" * 64)[0] == 403)
    e = raises(rest.result, "GET", "/workers/scripts/nope/settings", what="reading")
    check("a refusal: Cloudflare's code and message, never the token or the account's ID", e and "10007" in e and TOKEN not in e
          and ACCOUNT not in e, e)
    check("scrub: the account's ID and the token out of anything said", ACCOUNT not in rest.scrub(f"x {ACCOUNT} {TOKEN}")
          and TOKEN not in rest.scrub(f"x {ACCOUNT} {TOKEN}"))
    check("delete_script: True when deleted, False when there was none", rest.delete_script("mc-box") is True
          and rest.delete_script("mc-box") is False)
    check("an account ID that isn't one: refused", raises(capi.API, env={"CLOUDFLARE_API_TOKEN": TOKEN, "CLOUDFLARE_ACCOUNT_ID": "x"},
                                                           http=c.http) is not None)
    # the registries
    reg = images.Registries(rest, send=c.send)
    got = reg.copy_image("mc-box", FAKE_DIGEST)
    hub = [x for x in c.reg_log if x[0] == "cdn.example"]
    check("copy_image: the manifest and each part copied, by the pinned digest", got == FAKE_DIGEST
          and FAKE_DIGEST in c.repos["mc-box"] and len(c.blobs) == 2)
    check("...Docker Hub's download redirect followed with no credential (a stand-in asserts it)", len(hub) == 2
          and all("Authorization" not in x[3] for x in hub))
    check("...the registry's short-lived password in no log or message", all(c.pw not in json.dumps(x[3]) or x[0] == "registry.cloudflare.com"
                                                                            for x in c.reg_log))
    n = len(c.reg_log)
    reg.copy_image("mc-box", FAKE_DIGEST)
    check("...a second copy: nothing sent but the check that it's there", not any(x[1] == "PUT" for x in c.reg_log[n:]))
    c2 = CF()
    c2.tamper = True
    reg2 = images.Registries(rest_on(c2), send=c2.send)
    e = raises(reg2.copy_image, "mc-box", FAKE_DIGEST)
    check("...a part that isn't what the manifest says: refused before it's sent on, and nothing more copied",
          e and "isn't what its manifest says" in e and not c2.blobs and not c2.repos.get("mc-box"), e)
    e = raises(reg2.copy_image, "mc-box", "sha256:" + "0" * 64)
    check("...a manifest that isn't the pinned one: refused", e and "isn't the pinned one" in e, e)
    c3 = CF()
    c3.upload_location_host = "evil.example"
    e = raises(images.Registries(rest_on(c3), send=c3.send).copy_image, "mc-box", FAKE_DIGEST)
    check("...an upload address that isn't on registry.cloudflare.com: refused before the credentials go to it",
          e and "isn't on registry.cloudflare.com" in e and not any(x[0] == "evil.example" for x in c3.reg_log), e)
    c.repos["mc-box"]["rootfs-snapshot-1"] = ("application/vnd.oci.image.manifest.v1+json", json.dumps(
        {"config": {"mediaType": "application/vnd.cloudchamber.snapshot.config.v1+json"}, "layers": [{"size": 5}]}).encode())
    c.repos["mc-box"]["conv-1"] = ("application/vnd.oci.image.manifest.v1+json", json.dumps(
        {"config": {"mediaType": "application/vnd.cloudchamber.btrfs-chain.config.v1+json"}, "layers": [{"size": 7}]}).encode())
    ok, said = reg.delete_image("mc-box", FAKE_DIGEST)
    left = reg.residue("mc-box")
    check("delete_image: the image's manifest deleted (204 counts); what Cloudflare keeps listed by kind (a snapshot, "
          "the conversion)", ok and FAKE_DIGEST not in c.repos["mc-box"]
          and sorted(k for _t, k, _z in left) == ["conversion", "snapshot"], (said, left))
    check("...a snapshot's manifest refused by Cloudflare (403), the conversion (409): said, not deleted",
          reg.delete_manifest("mc-box", "rootfs-snapshot-1")[0] == 403 and reg.delete_manifest("mc-box", "conv-1")[0] == 409)


# --- the adapter --------------------------------------------------------------------------------------------------
def made(c, name="box", op=OP, key="c" * 64, **extra):
    s = computer(name, SSH_DIR="/nonexistent", **extra)
    rest = rest_on(c)
    a = cf.Cloudflare({name: s}, {}, rest=rest, images=images.Registries(rest, send=c.send))
    journal = []
    got = a.create(name, op=op, key=key, modules={"index.js": b"x", "logic.js": b"y"},
                   made=lambda what, v: journal.append(what))
    w = wire.Worker(got["url"], key, send=c.send)
    a.attach_worker(w)
    st = w.call("status")
    a.states[name] = dict(got, instance=cf.instance_id(got["worker"], st["do"]), op=op, do_id=st["do"])
    return a, got, journal


def adapter():
    print("\nthe adapter, on the stand-ins")
    old = cf.IMAGE_DIGEST
    cf.IMAGE_DIGEST = FAKE_DIGEST
    try:
        c = CF()
        a, got, journal = made(c)
        check("create: the Worker metacomputer-NAME, its image in a repository of that name, workers.dev only",
              got["worker"] == "metacomputer-box" and got["repo"] == "metacomputer-box"
              and got["url"] == f"https://metacomputer-box.{SUB}.workers.dev" and FAKE_DIGEST in c.repos["metacomputer-box"], got)
        check("...each part journaled as it's made: the image, the upload sent, the Worker, its namespace, its application",
              journal == ["made_image", "worker_sent", "made_worker", "namespace", "made_app"], journal)
        check("...the image prepared before the upload (polled while pending)", any(x[1].endswith("/image-preparations") for x in c.log)
              and not c.prepare[:-1])
        e = raises(a.create, "box", op="f" * 32, key="c" * 64, modules={"index.js": b"x"})
        check("...again over a Worker of that name: refused, nothing touched (never someone else's)", e and "leaves it alone" in e, e)
        c2 = CF()
        c2.ssh_on_create = True
        a2, _g, _j = made(c2)
        check("...an application whose SSH came back on: turned off, read back", c2.apps[a2.states["box"]["namespace"]]["configuration"]
              == {"wrangler_ssh": {"enabled": False}})
        c3 = CF()
        c3.prepare = ["error"]
        e = raises(made, c3)
        check("...an image Cloudflare can't prepare: refused before the Worker is uploaded", e and "didn't prepare" in e
              and not c3.scripts, e)
        # every operation checked through the Worker
        s = a._live("box")[0]
        check("_live: the Worker answers for this computer, its operation and the Durable Object recorded",
              s["computer"] == "box" and s["op"] == OP)
        do = c.dos["metacomputer-box"]
        for field, val in (("computer", "other"), ("op", "f" * 32), ("do", "0" * 64)):
            do.answer_as = {field: val}
            results = {m: raises(f, *args) for m, (f, args) in {
                "wake": (a.wake, ("box",)), "sleep": (a.sleep, ("box",)), "snapshot": (a.snapshot, ("box", "x")),
                "restore": (a.restore, ("box", "x")), "delete_snapshot": (a.delete_snapshot, ("box", "x")),
                "set_egress": (a.set_egress, ("box", ["a.org"])), "apply_egress": (a.apply_egress, ("box", "off")),
                "exec": (a.exec, ("box", ["id"])), "boot": (a.boot, ("box",)), "logs": (a.logs, ("box",)),
                "setup_done": (a.setup_done, ("box",)), "snapshots": (a.snapshots, ("box",))}.items()}
            check(f"...a Worker answering for another {field}: every operation refused, nothing done",
                  all(v and "leaves it alone" in v for v in results.values()) and not do.rec["running"], results)
        do.answer_as = None
        a_bad = cf.Cloudflare({"box": a.computers["box"]}, dict(a.states), worker=wire.Worker(got["url"], "d" * 64, send=c.send))
        e = raises(a_bad.wake, "box")
        check("...the Worker refusing the key (another Worker made under the name): refused, in words", e and "refuses this computer's key" in e, e)
        a_none = cf.Cloudflare({"box": a.computers["box"]}, dict(a.states))
        e = raises(a_none.wake, "box")
        check("...no key on this Mac: refused, saying so", e and "key isn't on this Mac" in e, e)
        check("the facade lib/remote.py asks (owned): the Worker's computer and operation for the recorded instance",
              a.api.instance(a.states["box"]["instance"])["name"] == "box" and a.api.instance(a.states["box"]["instance"])["op"] == OP)
        check("...a Worker refusing the key: another computer (owned refuses)", a_bad.api.instance(a.states["box"]["instance"])["name"] != "box")
        # lifecycle through the Worker
        e = raises(a.wake, "box")
        check("wake of a computer never started, with no snapshot: refused (its first start, from the image, is the "
              "build's alone)", e and "no snapshot" in e and not do.rec["running"], e)
        a._worker.call("start", {"from_image": True, "boot": False})
        out = a.wake("box")
        check("wake of a running computer: the boot script only (what runs is left running)", out["state"] == "awake"
              and do.rec["running"], out)
        e = raises(a.sleep, "box")
        check("sleep: the Worker's stop (a snapshot, then the container ended); the note says what's kept", e is None
              and do.rec["state"] == "stopped" and do.rec["latest"], e)
        out = a.wake("box")
        check("wake after a stop: from its latest snapshot, a note that programs closed", do.rec["running"] and "programs" not in str(out)
              or "every program closes" in str(out.get("note")), out)
        sn = a.snapshot("box", "ck1")
        check("checkpoint: a live snapshot (the computer keeps running)", sn["version"] and do.rec["running"]
              and do.rec["checkpoints"]["ck1"] == sn["version"], sn)
        check("snapshots: each checkpoint with its date", [x["label"] for x in a.snapshots("box")] == ["ck1"])
        a.restore("box", "ck1")
        check("restore: the Worker's restore (what runs discarded, started from it)", do.rec["latest"] == sn["version"])
        d = a.delete_snapshot("box", "ck1")
        check("delete_snapshot: forgotten, with the date Cloudflare keeps it until", d["expires"] and "ck1" not in do.rec["checkpoints"], d)
        e = a.set_egress("box", ["example.com"])
        check("egress: an allowlist set and read back as what's in force", e["outside"] == "allowlist" and e["allow"] == ["example.com"], e)
        e = a.apply_egress("box", "internet")
        check("...back to the internet: read back (the Worker restarted it)", e["outside"] == "internet" and e["restarted"], e)
        do.answer_as = {"egress": {"setting": "internet", "allow": [], "in_force": {"internet": True, "names": {"x.org": "allow"}}}}
        e = raises(a.apply_egress, "box", "off")
        do.answer_as = None
        check("...a read-back that isn't what was set: refused", e and "reads the egress back" in e, e)
        rc, so, se = a.exec("box", ["true"], input=b"in")
        check("exec: through the Worker, its standard input passed", rc == 0 and do.execs[-1] == (["true"], b"in"))
        a.setup_done("box")
        e = raises(a.exec, "box", ["id"])
        check("...after the setup: the Worker refuses it (403), said", e and "closed" in e, e)
        check("...the boot script and the logs are still there", a.boot("box")["booted_ms"] == 3000 and a.logs("box") == b"log line\n")
        st = a.status("box")
        check("status: awake, its Worker, its size, egress, x86_64, notes (the latest snapshot's date, the idle stop)",
              st["state"] == "awake" and st["worker"] == "metacomputer-box" and st["arch"] == "x86_64"
              and "kept until" in (st.get("note") or "") and "stops by itself" in (st.get("note") or ""), st)
        do.rec.update(running=False, state="stopped", lost={"at": 1795000000000, "since": 1794990000000})
        st = a.status("box")
        check("...a stop by Cloudflare said: what changed after its last snapshot was lost", "without warning" in (st.get("note") or "")
              and st["state"] == "stopped", st)
        # delete
        a.states["box"] = dict(a.states["box"])
        js = []
        out = a.delete("box", journal=js.append)
        check("delete: the plan journaled before anything is deleted; the Worker, its application and the image's "
              "manifest gone", js and js[0]["worker"] == "metacomputer-box" and out["gone"] and "metacomputer-box" not in c.scripts
              and not c.apps and FAKE_DIGEST not in c.repos["metacomputer-box"], (js, out))
        check("...what Cloudflare keeps: each snapshot with its date (from the Worker, before it went)",
              out["residue"] and all(r.get("expires") for r in out["residue"]), out["residue"])
        # never another's
        c4 = CF()
        a4, _g, _j = made(c4)
        c4.scripts["metacomputer-box"]["bindings"] = [b if b["name"] != "MCOMP_OP" else dict(b, text="f" * 32)
                                                       for b in c4.scripts["metacomputer-box"]["bindings"]]
        e = raises(a4.delete, "box")
        check("delete of a Worker whose bindings name another operation: refused, nothing deleted", e and "leaves it alone" in e
              and "metacomputer-box" in c4.scripts and c4.apps, e)
        a5 = cf.Cloudflare({"box": a4.computers["box"]}, dict(a4.states), worker=a4._worker)
        e = raises(a5.delete, "box")
        check("...without the account's token: refused, saying so", e and "CLOUDFLARE_API_TOKEN" in e, e)
        check("owned_lost: a Worker of the name with the computer and the operation: found", (lambda: (
            setattr(c4, "x", 0), a4.owned_lost("box", "f" * 32))[1])() == {"worker": "metacomputer-box"})
        e = raises(a4.owned_lost, "box", "1" * 32)
        check("...one with another operation: not taken for this launch's, left alone and said", e and "left alone" in e, e)
        check("...none: None", cf.Cloudflare({"box": computer("box")}, {}, rest=rest_on(CF())).owned_lost("box", OP) is None)
    finally:
        cf.IMAGE_DIGEST = old


# --- lib/remote.py with the hooks ------------------------------------------------------------------------------------
class Box:
    def __init__(self, td, name="box"):
        self.rd = os.path.join(td, ".run-vms", name)
        self.ssh = os.path.join(td, "ssh", name)
        self.logs = os.path.join(td, "logs", name)
        os.makedirs(self.rd)
        os.makedirs(self.ssh, mode=0o700)
        os.chmod(self.ssh, 0o700)
        self.s = dict(computer(name), RUN_DIR=self.rd, SSH_DIR=self.ssh, LOG_DIR=self.logs)


def hooks_tests(td):
    print("\nlib/remote.py with the provider's hooks")
    c = CF()
    started = []
    old_digest = cf.IMAGE_DIGEST
    cf.IMAGE_DIGEST = FAKE_DIGEST
    hooks.API_FACTORY = lambda creds: (rest_on(c), images.Registries(rest_on(c), send=c.send))
    hooks.WORKER_FACTORY = lambda url, key: wire.Worker(url, key, send=c.send)
    hooks.BROKER_FACTORY = lambda argv, cfg, log: started.append((argv, cfg, log))
    real_ask = bk.ask
    bk.ask = lambda sock, req, timeout=5.0: ({"ok": True, "iid": started[-1][1]["iid"]} if started and req["op"] == "ping"
                                              else None)
    old_gap, old_window = hooks.LOST_GAP_S, hooks.VISIBILITY_WINDOW_S
    hooks.LOST_GAP_S, hooks.VISIBILITY_WINDOW_S = 0.1, 0.3
    remote.CREDS.clear()
    remote.CREDS.update(CREDS)
    try:
        box = Box(td)
        kw = hooks.provider_kw(remote, box.s)
        a = cf.Cloudflare({"box": box.s}, {}, **kw)
        seen = {}
        real_create = a.create

        def spy(*args, **k):
            seen["journal"] = remote.read_state(box.rd)
            seen["key_mode"] = oct(os.stat(os.path.join(box.ssh, "cloudflare-key")).st_mode & 0o777)
            return real_create(*args, **k)
        a.create = spy
        hooks.create(remote, box.s, a, box.rd, {}, OP)
        rec = remote.read_state(box.rd)
        key = open(os.path.join(box.ssh, "cloudflare-key")).read().strip()
        check("create: the launch journaled (its operation, its Worker) before anything is made at Cloudflare",
              seen["journal"].get("launching") == OP and seen["journal"].get("worker") == "metacomputer-box")
        check("...the computer's key made first: 64 hex digits, 0600 beside its SSH keys", seen["key_mode"] == "0o600"
              and re.fullmatch(r"[0-9a-f]{64}", key) is not None)
        check("...then recorded: its identity (the Worker and its Durable Object), operation, address; the journal gone",
              rec.get("instance") == cf.instance_id("metacomputer-box", rec.get("do_id")) and rec.get("op") == OP
              and rec.get("url", "").endswith(".workers.dev") and "launching" not in rec, rec)
        check("...its key is the Worker's secret, uploaded once, and in nothing recorded",
              next(b["text"] for b in c.scripts["metacomputer-box"]["bindings"] if b["name"] == "MCOMP_KEY") == key
              and key not in json.dumps(rec))
        check("...its first start from the image (the internet on, for the setup), no boot yet",
              c.dos["metacomputer-box"].rec["running"] and c.dos["metacomputer-box"].rec["internet"])
        argv, cfg, log = started[-1]
        check("...the broker started with the key in its settings (its standard input), never its arguments; never the "
              "account's token", cfg["key"] == key and all(key not in x for x in argv) and TOKEN not in json.dumps(cfg)
              and ACCOUNT not in json.dumps(cfg) and argv[1].endswith("providers/cloudflare/broker.py") and argv[2:] == ["serve"], argv)
        check("...its settings: this computer, its Worker, identity and Durable Object", cfg["vm"] == "box" and cfg["worker"] == "metacomputer-box"
              and cfg["iid"] == rec["instance"] and cfg["do"] == rec["do_id"], cfg)
        os.environ["CLOUDFLARE_API_TOKEN"] = TOKEN
        check("a child of lib/remote.py never gets the account's credentials", "CLOUDFLARE_API_TOKEN" not in remote.child_env())
        del os.environ["CLOUDFLARE_API_TOKEN"]
        # the key file's rules
        os.chmod(os.path.join(box.ssh, "cloudflare-key"), 0o644)
        e = raises(hooks.read_key, box.s)
        os.chmod(os.path.join(box.ssh, "cloudflare-key"), 0o600)
        check("the key: one readable by others isn't used", e and "private" in e, e)
        lnk = Box(td, "linked")
        os.symlink(os.path.join(box.ssh, "cloudflare-key"), os.path.join(lnk.ssh, "cloudflare-key"))
        check("...nor a link", raises(hooks.read_key, lnk.s) is not None)
        check("...none: None (nothing asked)", hooks.read_key(Box(td, "nokey").s) is None)
        # without the token: daily use works with the key alone
        remote.CREDS.clear()
        kw2 = hooks.provider_kw(remote, box.s)
        check("without the account's token: the Worker (the key) and nothing else", "worker" in kw2 and "rest" not in kw2)
        a_daily = cf.Cloudflare({"box": box.s}, {"box": rec}, **kw2)
        a_daily.sleep("box")
        out = a_daily.wake("box")
        check("...stop and start need only the key", out["state"] == "awake" and c.dos["metacomputer-box"].rec["latest"])
        e = raises(a_daily.delete, "box")
        check("...delete needs the token: refused without it, nothing deleted", e and "CLOUDFLARE_API_TOKEN" in e and "metacomputer-box" in c.scripts, e)
        remote.CREDS.update(CREDS)
        # wake: the record checked first (owned), then the start, the broker
        a.states["box"] = rec
        n0 = len(started)
        c.dos["metacomputer-box"].rec.update(running=False, state="stopped")
        hooks.wake(remote, box.s, a, box.rd, rec)
        check("wake: started, the broker (its key), the record", c.dos["metacomputer-box"].rec["running"]
              and remote.read_state(box.rd).get("woken"))
        c.dos["metacomputer-box"].answer_as = {"op": "f" * 32}
        e = raises(hooks.wake, remote, box.s, a, box.rd, rec)
        c.dos["metacomputer-box"].answer_as = None
        check("...a Worker now answering for another operation: refused before anything (lib/remote.py owned)", e and "nothing" in e, e)
        # a lost upload, found by its bindings
        box2 = Box(td, "lost")
        c.lose_upload = True
        a2 = cf.Cloudflare({"lost": box2.s}, {}, **hooks.provider_kw(remote, box2.s))
        e = raises(hooks.create, remote, box2.s, a2, box2.rd, {}, OP)
        c.lose_upload = False
        j = remote.read_state(box2.rd)
        check("a launch whose upload answer was lost: the journal keeps its operation and that the upload was sent",
              j.get("launching") == OP and j.get("worker_sent") == "metacomputer-lost", (e, j))
        c.hidden = 2                       # Cloudflare's listing doesn't show it for two looks
        a2 = cf.Cloudflare({"lost": box2.s}, {}, **hooks.provider_kw(remote, box2.s))
        hooks.create(remote, box2.s, a2, box2.rd, remote.read_state(box2.rd), "a" * 32)
        check("...the next try looks again over the window, finds it by its bindings, deletes it, makes a new one",
              c.scripts["metacomputer-lost"]["bindings"] and any(b["text"] == "a" * 32 for b in c.scripts["metacomputer-lost"]["bindings"]
                                                                  if b["name"] == "MCOMP_OP"))
        box3 = Box(td, "foreign")
        c2 = CF()
        rest2 = rest_on(c2)
        rest2.upload("metacomputer-foreign", {"index.js": b"x"}, {"MCOMP_COMPUTER": "foreign", "MCOMP_OP": "e" * 32}, "c" * 64,
                     rest2.image_ref("x", FAKE_DIGEST), "standard-3")
        a3 = cf.Cloudflare({"foreign": box3.s}, {}, rest=rest2, images=images.Registries(rest2, send=c2.send))
        e = raises(hooks.create, remote, box3.s, a3, box3.rd, {"launching": OP, "worker_sent": "metacomputer-foreign"}, "b" * 32)
        check("...a Worker of that name without the operation's bindings is left alone, and said", e and "left alone" in e
              and "metacomputer-foreign" in c2.scripts, e)
        box4 = Box(td, "never")
        a4 = cf.Cloudflare({"never": box4.s}, {}, **hooks.provider_kw(remote, box4.s))
        remote.write_state(box4.rd, {"launching": OP, "worker_sent": "metacomputer-never"})
        t0 = time.monotonic()
        hooks.abort(remote, box4.s, a4, box4.rd, remote.read_state(box4.rd), OP, keep=False)
        check("abort of a launch that never shows: looked for over the whole window, then nothing to delete",
              time.monotonic() - t0 >= 0.3 and remote.read_state(box4.rd).get("aborted"))
        # abort: what this operation made
        a2.states["lost"] = remote.read_state(box2.rd)
        hooks.abort(remote, box2.s, a2, box2.rd, remote.read_state(box2.rd), "a" * 32, keep=False)
        check("abort: the Worker this build made, its application and image deleted; the record says so",
              "metacomputer-lost" not in c.scripts and remote.read_state(box2.rd).get("aborted")
              and not remote.read_state(box2.rd).get("instance") and not os.path.exists(os.path.join(box2.ssh, "cloudflare-key")))
        # plan, transport
        p = hooks.plan(remote, box.s, a, "outside=yes")
        check("plan: its own steps, x86_64, target cloudflare, no systemd or egress firewall, a connection held open",
              all(w in p for w in ("kind=provider", "arch=x86_64", "target=cloudflare", "systemd=no", "guest_firewall=no",
                                   "keep_awake=yes", "idle_stop=15")), p)
        check("...IDLE_STOP=off refused (6 hours at most)", raises(hooks.plan, remote, dict(box.s, IDLE_STOP="off"), a, "") is not None)
        args = type("A", (), {"run_dir": box.rd, "vm": "box"})()
        ep = transport.endpoint(box.s)
        w = hooks.connect_wrapper(remote, args, ep)
        check("the connection's check, from the record: --id IDENTITY --check N tunnel.py check --sock S --worker NAME",
              w[:2] == ("--id", rec["instance"]) and w[2] == "--check" and int(w[3]) == len(w) - 4
              and w[4] == TUNNEL and w[5] == "check" and w[6:] == ("--sock", ep["proxy"][3], "--worker", "metacomputer-box"), w)
        proxy, chk, tid = hooks.tunnel(remote, args, ep)
        check("the vault's binding: the tunnel, its check (the same program), the identity", proxy == ep["proxy"]
              and chk[0] == proxy[0] and chk[1] == "check" and tid == rec["instance"])
        check("...a record of another Worker: refused", raises(hooks.connect_wrapper, remote, args, dict(ep, host="other")))
        out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "remote.py"), "target", "--vm", "box", "--run-dir",
                              box.rd, "--user", "cua", "--transport", "cloudflare", "--setting", "CLOUDFLARE_WORKER=metacomputer-box"],
                             capture_output=True, env=dict(os.environ, **CREDS), timeout=60)
        txt = out.stdout.decode(errors="replace") + out.stderr.decode(errors="replace")
        check("remote.py target (run with the credentials in its environment): the ProxyCommand carries none of them, "
              "nor the key", out.returncode == 0 and "tunnel.py relay" in txt and "proxywrap.py --id" in txt
              and TOKEN not in txt and ACCOUNT not in txt and key not in txt, txt[-400:])
        # egress: widening only at the user's terminal
        import owners
        real_term = owners.at_user_terminal
        owners.at_user_terminal = lambda fds=(0,): False
        try:
            c.dos["metacomputer-box"].rec.update(internet=False, allow=[], running=True, state="running",
                                                 force={"internet": False, "names": {"metacomputer.invalid": "deny"}})
            ar = type("A", (), {"set": "internet", "setup": False})()
            e = raises(hooks.egress_check, remote, box.s, a, rec, ar)
            check("net egress internet from off, from an agent: refused (only at your own terminal)", e and "own terminal" in e, e)
            check("...net egress off from off: no question", hooks.egress_check(remote, box.s, a, rec,
                                                                                 type("A", (), {"set": "off", "setup": False})()) is None)
            al = type("A", (), {"show": False, "clear": False, "names": ["a.org"], "remove": False, "exact": False})()
            e = raises(hooks.cmd_allow, remote, box.s, a, rec, al)
            check("...net allow from off, from an agent: refused", e and "own terminal" in e, e)
            ar2 = type("A", (), {"set": "internet", "setup": True})()
            check("...the setup's own opening to internet, before it's set up: allowed", hooks.egress_check(remote, box.s, a, rec, ar2) is None)
            e = raises(hooks.egress_check, remote, box.s, a, dict(rec, setup_done="x"), ar2)
            check("...after it's set up: refused", e and "set up already" in e, e)
        finally:
            owners.at_user_terminal = real_term
        # exec's fixed forms
        buf = io.BytesIO()
        real_out = sys.stdout
        sys.stdout = type("O", (), {"buffer": buf, "flush": lambda self: None, "write": lambda self, x: None})()
        try:
            ex = type("A", (), {"argv": ["--", cf_boot(), "logs", "50"], "stdin": False, "timeout": 60, "max_output": 1 << 20})()
            hooks.cmd_exec(remote, box.s, a, rec, ex)
        finally:
            sys.stdout = real_out
        check("exec's fixed forms: BOOT logs N reads the Worker's logs (open after the setup too)", buf.getvalue() == b"log line\n")
        n = len(c.dos["metacomputer-box"].execs)
        hooks.cmd_exec(remote, box.s, a, rec, type("A", (), {"argv": [cf_boot(), "restart-desktop"], "stdin": False,
                                                             "timeout": 60, "max_output": 1})())
        check("...BOOT restart-desktop is the Worker's boot request", c.dos["metacomputer-box"].execs[n][0] == ["BOOT", "restart-desktop"])
        # delete: journaled, cut off and finished, what stays said and noted, the key gone
        rec = remote.read_state(box.rd)
        a.states["box"] = rec
        a._images = images.Registries(rest_on(c), send=c.send)
        c.repos["metacomputer-box"]["rootfs-snapshot-1"] = ("t", json.dumps(
            {"config": {"mediaType": "application/vnd.cloudchamber.snapshot.config.v1+json"}, "layers": [{"size": 5}]}).encode())
        c.fail_delete_app = 5
        e = raises(hooks.delete, remote, box.s, a, box.rd, rec, None)
        check("delete cut off (the application's deletion failing): refused, the plan journaled, the record kept",
              e and remote.read_state(box.rd).get("deleting", {}).get("worker") == "metacomputer-box"
              and remote.read_state(box.rd).get("instance"), e)
        c.fail_delete_app = 0
        err = io.StringIO()
        real_err = sys.stderr
        sys.stderr = err
        try:
            a2b = cf.Cloudflare({"box": box.s}, {"box": remote.read_state(box.rd)}, **hooks.provider_kw(remote, box.s))
            hooks.delete(remote, box.s, a2b, box.rd, remote.read_state(box.rd), None)
        finally:
            sys.stderr = real_err
        said = err.getvalue()
        check("...the next try finishes it: the Worker, its application, the image's manifest gone; the record says so",
              "metacomputer-box" not in c.scripts and not any(x["name"] == "metacomputer-box" for x in c.apps.values())
              and remote.read_state(box.rd).get("deleted") and not remote.read_state(box.rd).get("instance")
              and "deleting" not in remote.read_state(box.rd), said)
        check("...what Cloudflare keeps said plainly (its API can't delete it), with dates",
              "Cloudflare keeps, and its API can't delete" in said and "kept until" in said, said)
        note = os.path.join(box.logs, "cloudflare-residue.json")
        check("...and noted in the computer's log folder (vm delete keeps it), without its key or the account's ID",
              os.path.isfile(note) and key not in open(note).read() and ACCOUNT not in open(note).read())
        check("...its key removed", not os.path.exists(os.path.join(box.ssh, "cloudflare-key")))
    finally:
        cf.IMAGE_DIGEST = old_digest
        hooks.API_FACTORY = hooks.WORKER_FACTORY = hooks.BROKER_FACTORY = None
        bk.ask = real_ask
        hooks.LOST_GAP_S, hooks.VISIBILITY_WINDOW_S = old_gap, old_window
        remote.CREDS.clear()


def cf_boot():
    return "/opt/metacomputer/cloudflare/boot"


# --- the broker and tunnel.py -----------------------------------------------------------------------------------
class EchoWS:
    def __init__(self):
        self.q = queue.Queue()
        self.closed = False

    def send(self, data):
        self.q.put(bytes(data))

    def recv(self):
        d = self.q.get()
        return d

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)


class FakeWorker:
    def __init__(self, do, computer="box", op=OP, running=True, tcp_do=None):
        self.do, self.computer, self.op, self.running, self.tcp_do = do, computer, op, running, tcp_do
        self.tcps = 0

    def scrub(self, t, cap=300):
        return str(t)[:cap]

    def call(self, verb, body=None, timeout=60):
        return {"computer": self.computer, "op": self.op, "do": self.do, "running": self.running}

    def tcp(self):
        self.tcps += 1
        return EchoWS(), self.tcp_do or self.do


def tunnel_run(op, sock, worker, data=b"", timeout=30):
    return subprocess.run([sys.executable, TUNNEL, op, "--sock", sock, "--worker", worker], input=data,
                          capture_output=True, timeout=timeout)


def broker_tests(td):
    print("\nthe broker and tunnel.py")
    do = "d" * 64
    sockdir = tempfile.mkdtemp(prefix="mcb", dir="/tmp")
    os.chmod(sockdir, 0o700)
    sock = os.path.join(sockdir, "b.sock")
    iid = cf.instance_id("mc-box", do)
    fw = FakeWorker(do)
    b = bk.Broker(fw, {"vm": "box", "worker": "mc-box", "iid": iid, "do": do, "op": OP, "sock": sock})
    t = threading.Thread(target=b.serve, daemon=True)
    t.start()
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    try:
        check("the socket: 0600 in a 0700 folder", oct(os.stat(sock).st_mode & 0o777) == "0o600"
              and oct(os.stat(sockdir).st_mode & 0o777) == "0o700")
        r = tunnel_run("check", sock, "mc-box")
        check("tunnel.py check: the computer's identity as its Worker says it now", r.returncode == 0
              and json.loads(r.stdout) == {"id": iid, "state": "running"}, r)
        data = os.urandom(300_000)
        r = tunnel_run("relay", sock, "mc-box", data)
        check("tunnel.py relay: random bytes both ways through the broker and the Worker's websocket", r.returncode == 0
              and r.stdout == data, (r.returncode, len(r.stdout), r.stderr[-200:]))
        r = tunnel_run("relay", sock, "other")
        check("...for another Worker: refused by the broker", r.returncode == 1 and b"carries mc-box only" in r.stderr)
        fw.tcp_do = "0" * 64
        n = fw.tcps
        r = tunnel_run("relay", sock, "mc-box", b"secret bytes")
        fw.tcp_do = None
        check("...a connection that reached another Durable Object: closed before anything went through",
              r.returncode == 1 and b"another Durable Object" in r.stderr and r.stdout == b"" and fw.tcps == n + 1, r)
        fw.running = False
        b._cache = (0.0, None)
        n = fw.tcps
        r = tunnel_run("relay", sock, "mc-box")
        check("...a stopped computer: refused, never started (no connection asked)", r.returncode == 1 and b"is stopped" in r.stderr
              and fw.tcps == n)
        fw.running = True
        fw.op = "f" * 32
        b._cache = (0.0, None)
        r = tunnel_run("check", sock, "mc-box")
        check("...a Worker answering for another operation: its id is 'another' (proxywrap refuses)", json.loads(r.stdout)["id"] == "another")
        r = tunnel_run("relay", sock, "mc-box")
        check("...and no connection is made", r.returncode == 1 and b"doesn't answer for this computer" in r.stderr)
        fw.op = OP
        b._cache = (0.0, None)
        check("tick: ends when the computer is stopped and nothing is open; goes on while it runs",
              b.tick() is True and (setattr(fw, "running", False) or b.tick() is False))
        fw.running = True
        real_uid = bk.peer_uid
        bk.peer_uid = lambda conn: os.getuid() + 1
        r = tunnel_run("check", sock, "mc-box")
        bk.peer_uid = real_uid
        check("a peer of another account: refused before its request is read", r.returncode == 1)
        check("ping: its identity; quit: it ends, its socket gone", bk.ask(sock, {"op": "ping"})["iid"] == iid
              and bk.ask(sock, {"op": "quit"})["ok"] and (t.join(5) or not os.path.exists(sock)))
        r = tunnel_run("relay", sock, "mc-box")
        check("tunnel.py with no broker running: said plainly, no connection", r.returncode == 1 and b"isn't running" in r.stderr)
        r = subprocess.run([sys.executable, TUNNEL, "relay", "--sock", "rel.sock", "--worker", "mc-box"], capture_output=True)
        check("tunnel.py: a socket path that isn't absolute, or junk: refused (usage)", r.returncode == 2)
    finally:
        b.stopping.set()
        shutil.rmtree(sockdir, ignore_errors=True)
    src = open(TUNNEL).read()
    check("tunnel.py imports nothing from the checkout (the vault stages a copy) and never reads a key",
          "import cloud" not in src and "cloudflare-key" not in src and "sys.path" not in src)
    r = subprocess.run(["/usr/bin/python3", "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3, 9))", TUNNEL],
                       capture_output=True) if os.path.exists("/usr/bin/python3") else None
    check("...and parses as Python 3.9 (macOS's own python runs the vault's copy)", r is None or r.returncode == 0, r and r.stderr)
    p = subprocess.run([sys.executable, os.path.join(LAB, "providers", "cloudflare", "broker.py"), "serve"], input=b"{}",
                       capture_output=True, timeout=30)
    check("broker.py: settings that aren't complete: nothing started (exit 2)", p.returncode == 2)


# --- credentials ---------------------------------------------------------------------------------------------------
def credentials_off_env():
    print("\nthe credentials")
    for f in ("bin/metacomputer", "scripts/lib.sh", "scripts/remote-build.sh"):
        src = open(os.path.join(LAB, f)).read()
        line = next((ln for ln in src.splitlines() if ln.startswith("for mc_k in ")), "")
        check(f"{f}: the account's credentials taken off the environment first thing (the generated line)",
              "CLOUDFLARE_API_TOKEN" in line and "CLOUDFLARE_ACCOUNT_ID" in line and 'export -n "$mc_k"' in line)
    src = open(os.path.join(LAB, "providers", "cloudflare", "worker", "index.js")).read()
    check("the Worker passes no environment to the container (no binding, no key reaches it)", "env:" not in src.split("const opts")[1][:400])
    for f in ("broker.py", "tunnel.py", "wire.py"):
        src = open(os.path.join(LAB, "providers", "cloudflare", f)).read()
        check(f"providers/cloudflare/{f} never reads the account's token", "CLOUDFLARE_API_TOKEN" not in src)


# --- the guest -----------------------------------------------------------------------------------------------------
def guest(td):
    print("\nthe guest")
    boot = open(BOOT).read()
    # The boot's capability check, run as it is (its function), over a stand-in /proc and process table.
    fn = re.search(r"\ncaps_check\(\) \{\n.*?\n\}\n", boot, re.S).group(0)
    proc = os.path.join(td, "proc")
    bins = os.path.join(td, "bins")
    os.makedirs(bins)
    table = os.path.join(td, "table.json")

    def write_proc(procs):
        shutil.rmtree(proc, ignore_errors=True)
        for pid, (uids, caps_) in procs.items():
            os.makedirs(os.path.join(proc, str(pid)))
            with open(os.path.join(proc, str(pid), "status"), "w") as f:
                f.write(f"Name:\tx\nUid:\t{uids}\nCapInh:\t0000000000000000\nCapPrm:\t{caps_[0]}\nCapEff:\t{caps_[1]}\n"
                        f"CapBnd:\t00000000abac35fb\nCapAmb:\t{caps_[2]}\n")
        json.dump({str(p): int(u.split()[1]) for p, (u, _c) in procs.items()}, open(table, "w"))
    with open(os.path.join(bins, "ps"), "w") as f:
        f.write(f"#!/bin/sh\nexec {sys.executable} -c 'import json; [print(p, u) for p, u in json.load(open(\"{table}\")).items()]'\n")
    with open(os.path.join(bins, "pkill"), "w") as f:
        f.write(f"#!/bin/sh\necho \"pkill $*\" >> {td}/killed\n")
    for x in ("ps", "pkill"):
        os.chmod(os.path.join(bins, x), 0o755)
    prelude = (f'PATH="{bins}:$PATH"\nkill() {{ shift; for p in "$@"; do {sys.executable} -c "import json,sys; '
               f't=json.load(open(\'{table}\')); t.pop(sys.argv[1], None); json.dump(t, open(\'{table}\', \'w\'))" "$p"; '
               f'echo "kill $p" >> {td}/killed; done; }}\nfail() {{ echo "FAILED: $*"; exit 1; }}\n')
    script = prelude + fn.replace("/proc/[0-9]*/status", f"{proc}/[0-9]*/status").replace("${f#/proc/}", "${f#" + proc + "/}") + "caps_check\necho OK\n"

    def run(procs):
        write_proc(procs)
        try:
            os.remove(os.path.join(td, "killed"))
        except FileNotFoundError:
            pass
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        killed = open(os.path.join(td, "killed")).read() if os.path.exists(os.path.join(td, "killed")) else ""
        return r, killed, json.load(open(table))
    zero, full = "0000000000000000", "00000000abac35fb"
    r, killed, left = run({1: ("0 0 0 0", (full, full, full)), 50: ("1001 1001 1001 1001", (zero, zero, zero)),
                           60: ("1002 0 0 0", (full, full, zero))})
    check("the boot's capability check: root's processes and a sudo (euid 0) may hold capabilities; the desktop's none: "
          "it passes, nothing ended", r.returncode == 0 and "OK" in r.stdout and not killed, (r.stdout, r.stderr, killed))
    r, killed, left = run({1: ("0 0 0 0", (full, full, full)), 50: ("1001 1001 1001 1001", (zero, zero, full)),
                           51: ("1001 1001 1001 1001", (zero, zero, zero)), 70: ("998 998 998 998", (zero, zero, zero))})
    check("...a desktop process with an ambient capability (a command the platform ran as another user): sshd ended "
          "first, then every process of every account but root's, and the boot fails",
          r.returncode == 1 and "held capabilities" in r.stdout and killed.startswith("pkill -KILL -x sshd")
          and set(left) == {"1"}, (r.stdout, killed, left))
    r, killed, left = run({1: ("0 0 0 0", (full, full, full)), 50: ("1001 1001 1001 1001", (full, zero, zero))})
    check("...a permitted capability counts too", r.returncode == 1 and set(left) == {"1"})
    # the boot's other rules
    order = [boot.index(x) for x in ("# 3. The interception CA", "# 4. sshd", "# 5. The desktop", "\ncaps_check\nsay \"the desktop is ready")]
    check("the boot: the CA before sshd, sshd before the desktop, the capability check last", order == sorted(order))
    check("...the desktop started as its account with runuser, never the platform's run-as (which keeps capabilities)",
          "setsid runuser -u \"$U\" -- /usr/bin/Xvfb" in boot and "setsid runuser -u \"$U\" -- env -i" in boot)
    check("...sshd on the container's loopback and its own address (the platform's TCP route arrives there): the "
          "default route's device, loopback filtered out (found live: lo is 'global' there)",
          "ListenAddress 127.0.0.1" in boot and "ListenAddress $ADDR" in boot and "ip -4 route show default" in boot
          and "if ($4 !~ /^127\\./)" in boot)
    order0 = [boot.index(x) for x in ('[ "$(stat -c %u:%g /)" = 0:0 ] || chown 0:0 /', "# 4. sshd")]
    check(".../ made root's before sshd starts (the platform gives it an unmapped owner, and sshd's StrictModes then "
          "refuses the root-owned key lists: found live)", order0 == sorted(order0))
    check("...the platform's read-only /etc/hosts written only if the hostname is missing (found live)",
          "grep -qw -- \"$H\" /etc/hosts" in boot and "sed -i" not in boot.split("# 2. The hostname")[1].split("# 3.")[0])
    conf = open(os.path.join(GUEST, "rootfs", "etc", "ssh", "sshd_config.d", "05-metacomputer-cloudflare.conf")).read()
    check("...sshd's port 2222 (Cloudflare's own SSH would hold 22)", re.search(r"^Port 2222$", conf, re.M) is not None)
    check("...the interception CA trusted while it's there and taken out when it isn't",
          'install -m 0644 "$CA_SRC" "$CA_DST"' in boot and 'rm -f "$CA_DST"; update-ca-certificates --fresh' in boot)
    check("...quiesce: the desktop's programs ended, then sync (before a stop's snapshot)",
          re.search(r'quiesce \]; then\n.*end_desktop\n  sync', boot) is not None)
    for args, rc in (([], None), (["restart-desktop"], None), (["logs", "x"], 2), (["--evil"], 2), (["a", "b", "c"], 2)):
        if rc is None:
            continue
        r = subprocess.run(["/bin/bash", BOOT, *args], capture_output=True, timeout=30)
        check(f"...boot {' '.join(args)}: refused (exit {rc})", r.returncode == rc, r)
    r = subprocess.run(["/bin/bash", os.path.join(GUEST, "bootstrap.sh"), "--x"], capture_output=True, timeout=30)
    check("bootstrap.sh: an option refused before anything", r.returncode == 2)
    bs = open(os.path.join(GUEST, "bootstrap.sh")).read()
    check("bootstrap.sh: / made root's, and sshd's privilege separation folder made before sshd -t (found live)",
          bs.index("chown 0:0 /") < bs.index("sshd -t") and bs.index("install -d -m 0755 /run/sshd") < bs.index("sshd -t"))
    check("bootstrap.sh: Ubuntu 24.04 on amd64 in a Cloudflare container only; the host key reported last",
          '"${VERSION_ID:-}" = 24.04' in bs and "[ -d /run/cloudchamber ]" in bs and bs.rstrip().endswith('ssh_host_ed25519_key.pub)"'))
    prov = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    check("provision.sh: a cloudflare target, set up as the other providers' without systemd (nosysd), its own SSH "
          "settings, certutil for Chrome's store, Firefox told to trust the interception CA's copy",
          "nosysd() { [ \"$TARGET\" = sprite ] || [ \"$TARGET\" = vercel ] || [ \"$TARGET\" = cloudflare ]; }" in prov
          and "cloudflare_ssh" in prov and "libnss3-tools" in prov
          and "metacomputer-cloudflare-intercept.crt" in prov)
    fin = open(os.path.join(LAB, "provisioning", "guest", "finalize.sh")).read()
    adm = open(os.path.join(LAB, "provisioning", "guest", "rootfs", "usr", "local", "sbin", "metacomputer-admin")).read()
    check("finalize.sh and metacomputer-admin know a Cloudflare container", "/etc/metacomputer/cloudflare" in fin
          and "if [ -e /etc/metacomputer/cloudflare ]; then" in adm)


# --- the review's findings (audits/codex-cloudflare.md): each check tagged; a section that raises fails ------------------
def review_fixes():
    print("\nthe review's findings")
    old = cf.IMAGE_DIGEST
    cf.IMAGE_DIGEST = FAKE_DIGEST
    try:
        for tag, fn in (("[P2-create]", _create_race), ("[P2-stop]", _stop_honest), ("[P1]", _refresh_said),
                        ("[replay]", _nonce)):
            try:
                fn()
            except Exception as e:      # noqa: BLE001
                check(f"{tag} (the section raised: {type(e).__name__}: {str(e)[:120]})", False)
    finally:
        cf.IMAGE_DIGEST = old


def _another(c, name, op, computer="box"):
    """A Worker of `name` made by another operation (another vm create), straight into the stand-in."""
    c.scripts[name] = {"bindings": [{"name": "MCOMP_COMPUTER", "type": "plain_text", "text": computer},
                                    {"name": "MCOMP_OP", "type": "plain_text", "text": op},
                                    {"name": "MCOMP_KEY", "type": "secret_text", "text": "e" * 64}], "meta": {}, "modules": {}}
    c.namespaces[name] = hashlib.md5(f"other{name}".encode()).hexdigest()
    c.dos[name] = FakeDO(c, name)


def _create_race():
    other = "f" * 32
    c = CF()
    c.prepare = ["pending", "pending", "ready"]
    c.on_prepare = lambda cf_: (_another(cf_, "metacomputer-box", other) if "metacomputer-box" not in cf_.scripts else None)
    rest = rest_on(c)
    a = cf.Cloudflare({"box": computer("box", SSH_DIR="/nonexistent")}, {}, rest=rest, images=images.Registries(rest, send=c.send))
    journal = []
    e = raises(a.create, "box", op=OP, key="c" * 64, modules={"index.js": b"x", "logic.js": b"y"},
               made=lambda what, v: journal.append(what))
    puts = [x for x in c.log if x[0] == "PUT" and x[1].startswith(f"/accounts/{ACCOUNT}/workers/scripts/")]
    check("[P2-create] another operation's Worker of the name appears while the image is prepared: refused, nothing "
          "uploaded, its Worker untouched", e and "appeared" in e and not puts and "worker_sent" not in journal
          and next(b["text"] for b in c.scripts["metacomputer-box"]["bindings"] if b["name"] == "MCOMP_OP") == other, (e, journal))
    # The upload's read-back: another create (elsewhere) replaced it between the upload and the read.
    c2 = CF()
    real_route = c2.route

    def racing(method, p, qs, body, headers):
        out = real_route(method, p, qs, body, headers)
        if method == "PUT" and re.fullmatch(r"/workers/scripts/metacomputer-box", p):
            _another(c2, "metacomputer-box", other)
        return out
    c2.route = racing
    rest2 = rest_on(c2)
    a2 = cf.Cloudflare({"box": computer("box", SSH_DIR="/nonexistent")}, {}, rest=rest2, images=images.Registries(rest2, send=c2.send))
    journal2 = []
    e = raises(a2.create, "box", op=OP, key="c" * 64, modules={"index.js": b"x", "logic.js": b"y"},
               made=lambda what, v: journal2.append(what))
    check("[P2-create] ...replaced by another operation's right after the upload: caught by the read-back, refused, left alone",
          e and "another computer's or operation's bindings" in e and "made_worker" not in journal2
          and next(b["text"] for b in c2.scripts["metacomputer-box"]["bindings"] if b["name"] == "MCOMP_OP") == other, (e, journal2))
    # The lock on this Mac: a second create of the same Worker name waits for the first, and is refused when it can't.
    c3 = CF()
    rest3 = rest_on(c3)
    s3 = computer("race7", SSH_DIR="/nonexistent")
    a3 = cf.Cloudflare({"race7": s3}, {}, rest=rest3, images=images.Registries(rest3, send=c3.send))
    order = []

    def guard(worker):
        order.append(("guard", worker))
        return hooks.create_lock(remote, s3)(worker)
    real3 = c3.route

    def logged(method, p, qs, body, headers):
        if re.fullmatch(r"/workers/scripts/metacomputer-race7(/settings)?", p):
            order.append((method, p.rsplit("/", 1)[-1]))
        return real3(method, p, qs, body, headers)
    c3.route = logged
    d = cf.broker_paths(remote.LAB, "race7")[0]
    lockf = os.path.join(d, "create-metacomputer-race7.lock")
    old_wait = hooks.CREATE_LOCK_WAIT_S
    hooks.CREATE_LOCK_WAIT_S = 0.3
    import fcntl
    try:
        with hooks.create_lock(remote, s3)("metacomputer-race7"):
            fd = os.open(lockf, os.O_RDWR)
            try:
                held = raises(fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB, kind=OSError)
            finally:
                os.close(fd)
            e = raises(a3.create, "race7", op=OP, key="c" * 64, modules={"index.js": b"x", "logic.js": b"y"}, guard=guard)
        check("[P2-create] the check and the upload run under a lock on this Mac: a second create of the name while one "
              "holds it is refused, nothing uploaded", held is not None and e and "another vm create on this Mac" in e
              and "metacomputer-race7" not in c3.scripts, e)
        order.clear()
        got = a3.create("race7", op=OP, key="c" * 64, modules={"index.js": b"x", "logic.js": b"y"}, guard=guard)
        check("[P2-create] ...free: the name checked again under it, then the upload, then its read-back",
              got["worker"] == "metacomputer-race7" and order[order.index(("guard", "metacomputer-race7")) + 1:][:3]
              == [("GET", "settings"), ("PUT", "metacomputer-race7"), ("GET", "settings")], order)
        info = os.lstat(d)
        check("[P2-create] ...the lock in this account's private folder (0700), as the brokers' sockets are",
              info.st_uid == os.getuid() and (info.st_mode & 0o777) == 0o700 and os.path.isfile(lockf))
    finally:
        hooks.CREATE_LOCK_WAIT_S = old_wait
        try:
            os.unlink(lockf)
        except OSError:
            pass


def _stop_honest():
    c = CF()
    a, _got, _j = made(c)
    do = c.dos["metacomputer-box"]
    a._worker.call("start", {"from_image": True, "boot": False})
    do.fail_destroy = 1
    e = raises(a.sleep, "box")
    check("[P2-stop] a stop whose container Cloudflare doesn't end: an error that says the disk was saved, it still runs, "
          "nothing is lost, and what to do", e and e.startswith("box's disk was saved") and "still running" in e
          and "Nothing is lost" in e and "metacomputer status" in e, e)
    st = a.status("box")
    check("[P2-stop] ...status: stopping (not awake, not stopped), saying why", st["state"] == "stopping"
          and st["provider_state"] == "STOPPING" and "its disk was saved, but Cloudflare hasn't ended its container" in st.get("note", ""), st)
    doc = {d["check"]: d for d in a.doctor()}
    k = "box: its last stop ended the container"
    check("[P2-stop] ...cloud doctor: a failed check, saying so", k in doc and doc[k]["ok"] is False)
    out = a.sleep("box")
    check("[P2-stop] ...stop again once Cloudflare ends it: stopped", out["state"] == "stopped" and a.status("box")["state"] == "stopped", out)


def _refresh_said():
    c = CF()
    a, _got, _j = made(c)
    do = c.dos["metacomputer-box"]
    exp = int(time.time() * 1000) + 3 * 86400000
    do.refresh = [{"id": "snap-x", "labels": ["baseline"], "failures": 4, "error": "no capacity in the region",
                   "retry_at": exp - 86400000, "expires": exp, "expired": False}]
    st = a.status("box")
    check("[P1] status: a kept snapshot whose renewal keeps failing is said, by name, with its tries, its last error and "
          "the date it expires", "baseline: its snapshot couldn't be renewed (4 tries; last: no capacity in the region)" in st.get("note", "")
          and cf.days(exp) in st.get("note", ""), st.get("note"))
    doc = {d["check"]: d for d in a.doctor()}
    k = "box: its kept snapshots are renewed before they expire"
    check("[P1] ...cloud doctor: that check fails, saying so", doc[k]["ok"] is False and "couldn't be renewed" in doc[k]["detail"], doc[k])
    do.refresh = [{"id": "snap-y", "labels": ["latest"], "failures": 9, "error": "x", "expires": exp - 40 * 86400000, "expired": True}]
    check("[P1] ...one that expired: said as expired", "latest: its snapshot expired on" in a.status("box").get("note", ""))
    do.refresh = []
    doc = {d["check"]: d for d in a.doctor()}
    check("[P1] ...none failing: the check passes", doc[k]["ok"] is True, doc[k])


def _nonce():
    key = "c" * 64
    h1 = wire.sign(key, "POST", "/v1/status", "", b"{}", now=1800000000)
    h2 = wire.sign(key, "POST", "/v1/status", "", b"{}", now=1800000000)
    check("[replay] every signed request carries a new nonce: the same request twice in one second is signed twice "
          "differently", h1 != h2 and all(re.fullmatch(r"v1\.1800000000\.[0-9a-f]{24}\.[0-9a-f]{64}", h) for h in (h1, h2)), (h1, h2))
    c = CF()
    a, _got, _j = made(c)
    ok = [a._worker.call("status") for _ in range(3)]
    check("[replay] ...so a Worker that refuses a nonce it has seen takes each of them", len(ok) == 3 and len(c.nonces) >= 4)


def main():
    td = tempfile.mkdtemp(prefix="mcomp-cf-")
    try:
        worker_js()
        transport_and_policy()
        manifest()
        wire_tests()
        api_and_images()
        adapter()
        hooks_tests(td)
        broker_tests(td)
        credentials_off_env()
        guest(td)
        review_fixes()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ": all passed"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
