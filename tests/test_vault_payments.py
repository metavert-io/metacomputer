#!/usr/bin/env python3
"""Offline checks for paying for things: cards in the vault (`secrets set --kind card`, `policy --pay`), purchases (`secrets
pay`), caps and the spending record, the audit trail, merchants' agent rules (/agents.md, the UCP profile), the hub's card
masks (lib/cardmask.py, mcp_hub's on_upstream), Shop spend-request cards (`shop-card`) and Shop checkouts (`shop-complete`,
lib/shop.py) through the Shop CLI the user pins (`shop-cli set`), AP2 mandates (`ap2`) and Web Bot Auth (`wba`).

No VM needed; the approach of tests/test_vault_codes.py: a TEST build of mcomp-vault, an ssh stub standing in for the VM
(the fill account's read, check and fill, held to top_origins as the VM's helper is, and the desktop account's human-mode
script), every passphrase and card part typed into a pseudo-terminal, approvals typed at the unlock terminal (never a
Touch ID dialog: a test build has none). Card numbers are test numbers only (4242 4242 4242 4242, 4000 0566 5566 5556,
5555 5555 5555 4444); the merchants are a server on this Mac's loopback (MCOMP_VAULT_TEST_FETCH_LOOPBACK, test builds
only) and a stand-in for Shopify's `shop` CLI. The checks named "F1" to "F16" are the fixes for the audit's findings.

Optional: MCOMP_AP2_SDK_PYTHON (a Python with AP2's SDK dependencies) and MCOMP_AP2_SDK_PATH (a checkout of
google-agentic-commerce/AP2's code/sdk/python) also verify the AP2 mandates with AP2's own verifier.

Run: python3 tests/test_vault_payments.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build python3 tests/test_vault_payments.py
"""
import base64
import fcntl
import glob
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402
import test_vault_codes as tvc  # noqa: E402
import test_vault_ops as tvo  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
NUMBER = "4242424242424242"         # a test card number (it passes the card check; no real card has it)
NUMBER2 = "4000056655665556"
HOLDER = "Ada Testbuyer Qz"
ZIP = "QZ9 7X1"
SHOP_NUMBER = "5555555555554444"
SHOP_HOLDER = "Shop Holder Vq"
VALUES = [PASS, NUMBER, NUMBER2, "4242 4242 4242 4242", HOLDER, ZIP, SHOP_NUMBER, SHOP_HOLDER, "11/30", "11/2030"]
outputs = []

STUB = r'''#!{python}
# Stands in for /usr/bin/ssh in a TEST build of mcomp-vault: the fill account's secret-fill (read, check, fill) and the
# desktop account's human-mode script (total, check, fill). Records what it got; answers like the VM's helpers, which hold
# the tab itself to top_origins (a payment frame's site is never the tab) and say so (top_checked).
import json, os, sys, time
LOG = {log!r}
d = os.path.dirname(LOG)
argv = sys.argv[1:]
remote = argv[-1] if argv else ""
dest = next((a for a in argv if a.endswith("@127.0.0.1")), "")
user = dest.split("@")[0]
def flag(n):
    p = os.path.join(d, n)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False
def val(n, default=None):
    p = os.path.join(d, n)
    return open(p).read() if os.path.exists(p) else default
raw = sys.stdin.buffer.read()
try:
    req = json.loads(raw)
except ValueError:
    req = {{}}
rec = {{"user": user, "mode": req.get("mode"), "what": req.get("what"), "field": req.get("field"), "origins": req.get("origins"),
       "top_origins": req.get("top_origins"), "selector": req.get("selector"), "human": "mcomp-human-fill" in remote,
       "has_value": bool(req.get("value")), "value": req.get("value")}}
with open(LOG, "a") as f:
    f.write(json.dumps(rec) + "\n")
v = req.get("value") or ""
origins = req.get("origins") or ["?"]
tops = req.get("top_origins") if "top_origins" in req else origins
ack = "top_origins" in req and not os.path.exists(os.path.join(d, "no-ack"))
if rec["human"]:
    origin = val("human-origin", origins[0])
    if req.get("mode") == "total":
        texts = json.loads(val("human-totals", '["Total $9.99"]'))
        out = {{"ok": True, "mode": "total", "origin": origin, "texts": texts, "befores": json.loads(val("human-befores", json.dumps([""] * len(texts))))}}
    elif origin not in origins:
        out = {{"ok": False, "denied": True, "reason": "the human-mode site is " + origin + ", which isn't allowed here"}}
    elif req.get("mode") == "check":
        out = {{"ok": True, "mode": "check", "field": req.get("field"), "origin": origin}}
    else:
        out = {{"ok": True, "mode": "fill", "field": req.get("field"), "origin": origin, "chars": len(v), "route": "keyboard"}}
elif user == "mcfill":
    mode = req.get("mode")
    origin = val("tab-origin", tops[0])
    if mode == "fill" and os.path.exists(os.path.join(d, "move-on-fill")):
        origin = val("move-on-fill")           # the tab moved between the check and the fill
    if origin not in tops:
        # As the helper does: no tab on the sites the tab may be on (a frame's site isn't one of them).
        out = {{"ok": False, "denied": True, "reason": "no open tab on " + ", ".join(tops) + " in the driver's Chrome; open the site first"}}
    elif mode == "read":
        out = {{"ok": True, "mode": "read", "origin": origin, "text": val("read-text", "Total $12.34")}}
        if req.get("what") == "total":
            out["visible"] = val("total-visible", "1") == "1"
    elif flag("swap-next"):
        out = {{"ok": True, "mode": mode, "field": req.get("field"), "origin": "https://evil.example"}}
    elif v and flag("fail-after-value-next"):
        # A page that took the value and then changed the field: the fill reports a failure, but the value went.
        out = {{"ok": False, "denied": False, "origin": origin, "reason": "the page changed during the fill; the value may have gone elsewhere"}}
    elif mode == "check":
        if os.path.exists(os.path.join(d, "hold-check")):
            # The check waits (a slow page) until the test releases it: the policy can change meanwhile.
            open(os.path.join(d, "holding"), "w").close()
            for _ in range(300):
                if os.path.exists(os.path.join(d, "release")):
                    break
                time.sleep(0.1)
            for n in ("hold-check", "holding", "release"):
                if os.path.exists(os.path.join(d, n)):
                    os.remove(os.path.join(d, n))
        out = {{"ok": True, "mode": "check", "field": req.get("field"), "origin": origin}}
    else:
        if os.path.exists(os.path.join(d, "hold-fill")):
            # The fill itself takes a while (the test checks the hub's masks meanwhile) until the test releases it.
            open(os.path.join(d, "holding-fill"), "w").close()
            for _ in range(500):
                if os.path.exists(os.path.join(d, "release-fill")):
                    break
                time.sleep(0.1)
            for n in ("hold-fill", "holding-fill", "release-fill"):
                if os.path.exists(os.path.join(d, n)):
                    os.remove(os.path.join(d, n))
        out = {{"ok": True, "mode": "fill", "field": req.get("field"), "origin": origin, "chars": len(v), "route": "element"}}
    if ack and out.get("ok") is True:
        out["top_checked"] = True
else:
    out = {{}}
print(json.dumps(out))
'''

# Stands in for Shopify's `shop` CLI (@shopify/shop), run by the vault with the Node the user pinned: spend-request get
# --output-file, checkout get|complete, account. Its data lives in another folder (the pinned package must not change). It
# records its arguments, environment, working folder and Node's own arguments; with try-planted it loads a module that
# isn't part of its package (one an agent put in a node_modules folder above it), which a confined run never lets load.
# Long on purpose (over 256 bytes before the code): `shop-cli set` reads the runtime from its first line.
SHOP_STUB = r'''#!/usr/bin/env node
// A stand-in for Shopify's shop CLI, for metacomputer's vault tests: it answers like the real one, from files the test
// writes, and never talks to Shopify. Every call is recorded (arguments, environment, working folder, Node's own flags).
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { Worker } from "node:worker_threads";
import { spawnSync } from "node:child_process";
const d = __DIR__;
const a = process.argv.slice(2);
const has = (n) => fs.existsSync(path.join(d, n));
const val = (n, def) => (has(n) ? fs.readFileSync(path.join(d, n), "utf8").trim() : def);
const stdin = a.includes("-") ? fs.readFileSync(0, "utf8") : "";
fs.appendFileSync(path.join(d, "calls.jsonl"), JSON.stringify({ argv: a, stdin, env: process.env, cwd: process.cwd(), execArgv: process.execArgv }) + "\n");
if (has("try-planted")) {
  createRequire(import.meta.url)("planted-helper");
}
if (has("try-escapes")) {
  // What the re-check showed the module hooks alone don't stop: an eval worker importing a file outside the package, a
  // child process, a read of a file outside it. Each is recorded as it went.
  const out = {};
  const outside = val("try-escapes");
  out.evalWorker = await new Promise((res) => {
    const w = new Worker("import(" + JSON.stringify(outside + "/escape.mjs") + ").then(m => require('worker_threads').parentPort.postMessage(String(m.default)), e => require('worker_threads').parentPort.postMessage('DENIED ' + (e.code || e.message)))", { eval: true });
    w.on("message", res); w.on("error", (e) => res("DENIED " + (e.code || e.message)));
  });
  const sp = spawnSync("/bin/sh", ["-c", "echo spawned > " + JSON.stringify(outside + "/spawn-ran")]);
  out.spawn = sp.error ? "DENIED " + (sp.error.code || sp.error.message) : "RAN status " + sp.status;
  try { out.readPlanted = "READ " + fs.readFileSync(outside + "/planted.txt", "utf8").trim(); } catch (e) { out.readPlanted = "DENIED " + e.code; }
  fs.appendFileSync(path.join(d, "calls.jsonl"), JSON.stringify({ escapes: out }) + "\n");
}
if (has("try-network")) {
  // Network from inside the sandbox: a local service directly, a Unix socket, and through the run's proxy a local
  // address, a private one, and the one stand-in merchant the test build allows.
  const cfg = JSON.parse(val("try-network"));
  const net = await import("node:net");
  const direct = (opts) => new Promise((res) => {
    const s = net.connect(opts);
    const t = setTimeout(() => { s.destroy(); res("TIMEOUT"); }, 3000);
    s.on("connect", () => { clearTimeout(t); s.destroy(); res("CONNECTED"); });
    s.on("error", (e) => { clearTimeout(t); res("DENIED " + e.code); });
  });
  const proxy = new URL(process.env.HTTPS_PROXY || "http://127.0.0.1:9");
  // The run's secret, as Node sends the proxy URL's userinfo (Proxy-Authorization: Basic).
  const goodAuth = "Basic " + Buffer.from(decodeURIComponent(proxy.username) + ":" + decodeURIComponent(proxy.password)).toString("base64");
  const tunnel = (target, auth = goodAuth) => new Promise((res) => {
    const s = net.connect({ host: proxy.hostname, port: Number(proxy.port) });
    let buf = "", done = false;
    const fin = (v) => { if (!done) { done = true; s.destroy(); res(v); } };
    setTimeout(() => fin("TIMEOUT"), 8000);
    s.on("connect", () => s.write("CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n" + (auth ? "Proxy-Authorization: " + auth + "\r\n" : "") + "\r\n"));
    s.on("data", (d) => {
      buf += d;
      if (!buf.includes("\r\n\r\n")) return;
      const status = buf.split(" ")[1];
      if (status !== "200") return fin(status);
      s.removeAllListeners("data");
      let body = "";
      s.on("data", (x) => { body += x; });
      s.on("end", () => fin("200 " + body.split("\r\n")[0]));
      s.write("GET /agents.md HTTP/1.0\r\nHost: stand-in\r\n\r\n");
    });
    s.on("error", (e) => fin("DENIED " + e.code));
  });
  const out = {};
  out.directLocal = await direct({ host: "127.0.0.1", port: cfg.local_port });
  out.unixSocket = await direct({ path: cfg.unix_socket });
  out.proxyLocal = await tunnel("127.0.0.1:" + cfg.other_port);
  out.proxyPrivate = await tunnel("10.0.0.1:443");
  out.proxyAllowed = await tunnel(cfg.allowed);
  out.proxyNoAuth = await tunnel(cfg.allowed, "");
  out.proxyBadAuth = await tunnel(cfg.allowed, "Basic " + Buffer.from("mc:not-the-secret").toString("base64"));
  // Its connection cap: 8 connections that send nothing, then a ninth.
  const idle = [];
  for (let i = 0; i < 8; i++) { const s = net.connect({ host: proxy.hostname, port: Number(proxy.port) }); s.on("error", () => {}); idle.push(s); }
  await new Promise((r) => setTimeout(r, 500));
  out.proxyOverCap = await tunnel(cfg.allowed);
  for (const s of idle) s.destroy();
  out.proxyEnv = process.env.HTTPS_PROXY ? "set" : "";
  fs.appendFileSync(path.join(d, "calls.jsonl"), JSON.stringify({ network: out }) + "\n");
}
if (has("try-public")) {
  // Optional (MCOMP_TEST_PUBLIC_NET=1): a real HTTPS request through the proxy, the way Shop's CLI makes them.
  const https = await import("node:https");
  const tls = await import("node:tls");
  // As Shop's CLI does (lib/transport.mjs, installTlsProfile): Node's bundled roots, plus the OS store when it can be read.
  let system = [];
  try { system = tls.getCACertificates("system"); } catch {}
  tls.setDefaultCACertificates([...tls.getCACertificates("bundled"), ...system]);
  const r = await new Promise((res) => {
    const req = https.get(val("try-public"), { agent: new https.Agent({ proxyEnv: { HTTPS_PROXY: process.env.HTTPS_PROXY } }) }, (x) => { x.resume(); res("HTTP " + x.statusCode); });
    req.on("error", (e) => res("ERROR " + (e.code || e.message)));
    req.setTimeout(15000, () => { req.destroy(); res("TIMEOUT"); });
  });
  fs.appendFileSync(path.join(d, "calls.jsonl"), JSON.stringify({ publicNet: r }) + "\n");
}
const say = (o) => { process.stdout.write(JSON.stringify(o) + "\n"); process.exit(o.ok ? 0 : 1); };
if (a[0] === "spend-request" && a[1] === "get") {
  const out = a[a.indexOf("--output-file") + 1];
  if (has("not-approved")) say({ ok: false, v: 1, error: { code: "not_approved", message: "dummy", retryable: true } });
  if (fs.existsSync(out)) say({ ok: false, v: 1, error: { code: "file_exists" } });
  const fd = fs.openSync(out, "wx", 0o600);
  const sid = has("wrong-id") ? "another-request-0000" : a[2];
  fs.writeSync(fd, JSON.stringify({ spend_request_id: sid, card: { id: "c1", type: "card", display: { last_digits: "4444" },
    credential: { type: "pan", number: __NUMBER__, expiry_month: "11", expiry_year: "2030", cvv: "731", name: __HOLDER__ },
    billing_address: { postal_code: __ZIP__ } } }));
  fs.closeSync(fd);
  const data = { status: "consumed", credential_output_file: out, business_name: "Stand-in Store", currency: "USD", approval_limit: 2500 };
  if (!has("no-business-url")) data.business_url = val("business-url");
  say({ ok: true, v: 1, data });
}
if (a[0] === "account") say({ ok: true, v: 1, data: { signed_in: true, buyer: { email: "never-print@example.com" } } });
if (a[0] === "checkout" && a[1] === "get") {
  const n = Number(val("gets", "0")) + 1;
  fs.writeFileSync(path.join(d, "gets"), String(n));
  let total = Number(val("checkout-total", "1579"));
  if (n >= 2 && has("reprice-after-first")) total += 500;          // the merchant repriced it while the user was being asked
  const quantity = has("requantity-after-first") ? (n >= 2 ? 1.9 : 1.1) : (has("requantity-big") ? "__BIG__" : 1);
  const body = JSON.stringify({ ok: true, v: 1, data: { id: a[2], status: val("checkout-status", "ready_for_complete"), currency: "USD",
    buyer: { email: "never-print@example.com" }, line_items: [{ id: "line-a", quantity }],
    totals: [{ type: "subtotal", amount: total - 129 }, { type: "total", amount: total }],
    payment: { instruments: [has("instrument-big") ? { id: "instrument-a", handler_id: "shop_pay", type: "shop_pay", n: "__IBIG__" } : { id: "instrument-a", handler_id: "shop_pay", type: "shop_pay" }] },
    continue_url: "https://stand-in-store.example/checkouts/x" } });
  // Two integers past what a 64-bit number holds, written as JSON numbers (JavaScript can't hold them, so as text).
  process.stdout.write(body.replace('"__BIG__"', n >= 2 ? "18446744073709551617" : "18446744073709551616").replace('"__IBIG__"', "18446744073709551616") + "\n");
  process.exit(0);
}
if (a[0] === "checkout" && a[1] === "complete") {
  if (has("complete-garbage")) { process.stdout.write("this is not an answer\n"); process.exit(0); }
  if (has("complete-envelope")) { process.stdout.write(val("complete-envelope") + "\n"); process.exit(1); }
  if (has("hold-complete")) {
    // The completion takes a while (the test changes something meanwhile): wait for its word.
    fs.writeFileSync(path.join(d, "holding-complete"), "1");
    for (let i = 0; i < 300 && !has("release-complete"); i++) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100);
    for (const f of ["hold-complete", "holding-complete", "release-complete"]) { try { fs.unlinkSync(path.join(d, f)); } catch {} }
  }
  const data = { status: "completed", order: { id: "gid://shopify/Order/1", label: "#1042" } };
  if (!has("complete-no-total")) {
    data.totals = [{ type: "total", amount: Number(val("complete-total", val("checkout-total", "1579"))) }];
    data.currency = val("complete-currency", "USD");
    if (has("complete-total-currency")) data.totals[0].currency = val("complete-total-currency");
  }
  say({ ok: true, v: 1, data });
}
say({ ok: false, v: 1, error: { code: "unknown" } });
'''


class Merchant(http.server.BaseHTTPRequestHandler):
    """A stand-in merchant: /agents.md and /.well-known/ucp as the test sets them; records each request's headers."""

    def do_GET(self):
        self.server.got.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        body, status, ctype = None, 404, "text/plain"
        if self.path == "/agents.md" and self.server.agents is not None:
            body, status, ctype = (self.server.agents if isinstance(self.server.agents, bytes) else self.server.agents.encode()), 200, "text/markdown"
            ctype = getattr(self.server, "agents_ctype", None) or ctype
        elif self.path == "/.well-known/ucp" and self.server.ucp is not None:
            body, status, ctype = json.dumps(self.server.ucp).encode(), 200, "application/json"
        body = body or b"not here"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


SHOPIFY_AGENTS = """# Agents
This store is powered by Shopify. Use the checkout tools.
Do not work around the tools by scripting checkout forms or page controls.
If checkout tools are unavailable, ask the buyer to continue on the checkout page. Do not fall back to scripted form fills.
"""
OPEN_AGENTS = "# Agents\nYou may browse, compare and buy for a buyer who asked you to.\n"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def es256_verify(jwk, signing_input, sig):
    """ES256 with openssl (a P-256 key from a JWK; the JWS signature r||s turned into DER)."""
    x, y = b64u_dec(jwk["x"]), b64u_dec(jwk["y"])
    spki = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + b"\x04" + x + y

    def der_int(b):
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    seq = der_int(sig[:32]) + der_int(sig[32:])
    der = b"\x30" + bytes([len(seq)]) + seq
    with tempfile.TemporaryDirectory() as td:
        pem = os.path.join(td, "k.pem")
        open(pem, "w").write("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(spki).decode() + "\n-----END PUBLIC KEY-----\n")
        open(os.path.join(td, "s"), "wb").write(der)
        open(os.path.join(td, "m"), "wb").write(signing_input)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pem, "-signature", os.path.join(td, "s"), os.path.join(td, "m")],
                           capture_output=True, text=True)
        return r.returncode == 0 and "Verified OK" in r.stdout


def sd_parts(token):
    """(header, payload, disclosures decoded, signing input, signature) of a compact SD-JWT."""
    parts = token.split("~")
    jws = parts[0].split(".")
    discs = [p for p in parts[1:] if p]
    return (json.loads(b64u_dec(jws[0])), json.loads(b64u_dec(jws[1])),
            {b64u(hashlib.sha256(d.encode()).digest()): json.loads(b64u_dec(d)) for d in discs},
            (jws[0] + "." + jws[1]).encode(), b64u_dec(jws[2]))


def find_node():
    """A Node of 22.15 or newer (module.registerHooks) for the stand-in Shop CLI: MCOMP_TEST_NODE, node on PATH, or
    Homebrew's; None when there's none (the Shop checks are then skipped, and say so)."""
    for c in [os.environ.get("MCOMP_TEST_NODE"), shutil.which("node")] + sorted(glob.glob("/opt/homebrew/opt/node*/bin/node"), reverse=True):
        if c and os.path.exists(c):
            r = subprocess.run([c, "-e", "process.exit(typeof require('node:module').registerHooks === 'function' ? 0 : 3)"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return c
    return None


def display_id(key):
    return hashlib.sha256((key or "").encode()).hexdigest()[:8]


SESSION_MARK = "MCOMP_TEST_PAY_SESSION"


def in_agent_session():
    """Runs this test again under a stand-in agent session (a process named claude), so every command it starts belongs
    to one session whatever runs it: Claude Code, Codex or a plain terminal. A purchase is bound to the session that
    started it, and one started outside any can't be resumed. None when already inside (or when it can't be built)."""
    if os.environ.get(SESSION_MARK):
        return None
    d = tempfile.mkdtemp(prefix="mcvs-", dir="/tmp")
    try:
        fake = os.path.join(d, "claude")
        open(fake + ".c", "w").write(tvo.FAKE_CLAUDE_C)
        if subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True).returncode != 0:
            return None
        return subprocess.run([fake, os.getcwd(), sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]],
                              env=dict(os.environ, **{SESSION_MARK: "1"})).returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)


ORPHAN_PY = r"""
import os, subprocess, sys, time
if os.fork():
    os._exit(0)
t = time.time() + 10
while os.getppid() != 1 and time.time() < t:
    time.sleep(0.01)
r = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL)
print("__rc=%d" % r.returncode, flush=True)
"""


def orphan(argv, env, timeout=120):
    """A command with no agent session above it: launchd adopted the process that runs it. (exit status, its output)."""
    p = subprocess.Popen([sys.executable, "-I", "-c", ORPHAN_PY, *argv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    out = p.communicate(timeout=timeout)[0].decode(errors="replace")
    m = re.search(r"__rc=(\d+)\s*$", out)
    return (int(m[1]) if m else -1), out[:m.start()] if m else out


def main():
    td = tempfile.mkdtemp(prefix="mcvp-", dir="/tmp")
    procs = []
    try:
        offline_checks(td)
        run(td, procs)
    finally:
        for p in procs:
            try:
                if isinstance(p, http.server.HTTPServer):
                    p.shutdown()
                    continue
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
            if isinstance(p, ts.Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if ts.results and all(ts.results) else 1


def offline_checks(td):
    """The hub's card masks (lib/cardmask.py and mcp_hub's on_upstream), and the human-mode script's total (vault/Human.swift)
    against a fake accessibility tree."""
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import cardmask
    M = cardmask.CARD_MASK
    check("the hub hides a card number a page shows (plain, spaced or dashed), and only numbers that pass the card check",
          cardmask.mask("entry \"4242424242424242\" focused")[0] == f"entry \"{M}\" focused"
          and cardmask.mask("Card 4242 4242 4242 4242.")[0] == f"Card {M}." and cardmask.mask("5555-5555-5555-4444")[0] == M
          and cardmask.mask("4242424242424241")[0] == "4242424242424241" and cardmask.mask("order 1234567890123452")[0] == "order 1234567890123452"
          and cardmask.mask("t=1732320000000 id 42")[0] == "t=1732320000000 id 42")
    # F6: separators, a number padded inside a longer run, split JSON strings, numbers, the 2-series, any depth.
    nbsp, zw = " ", "​"
    variants = ["4242  4242  4242  4242", f"4242{nbsp}4242{nbsp}4242{nbsp}4242", "4242.4242.4242.4242", "4242/4242/4242/4242",
                f"4242{zw}4242{zw}4242{zw}4242", "\"4242\",\"4242\",\"4242\",\"4242\"", "2223 0031 2200 3222"]
    check("F6: the mask covers double and non-breaking spaces, dots, slashes, zero-width spaces, a number split across a JSON "
          "list's strings, and Mastercard's 2-series",
          all(M in cardmask.mask(v)[0] and "4242" not in cardmask.mask(v)[0].replace(M, "") for v in variants[:-1])
          and M in cardmask.mask(variants[-1])[0], [cardmask.mask(v)[0] for v in variants])
    for part, fn in (("the hub's masks", offline_masks), ("the hub's order", offline_hub), ("the hub's acks", offline_acks)):
        try:
            fn(td, cardmask, M)
        except Exception as e:      # noqa: BLE001 - an older lib/: the checks after it in that part fail, the run goes on
            check(f"[{part}] ran to its end", False, repr(e)[:300])
    try:
        offline_human()
    except Exception as e:          # noqa: BLE001 - an older helper: its checks fail, the run goes on
        check("[human mode's helper] ran to its end", False, repr(e)[:300])


def offline_masks(td, cardmask, M):
    msg = {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "bad value 4242 4242 4242 4242", "data": {"n": 4000056655665556}}}
    note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": 1, "message": "typed 4242424242424242"}}
    res = {"content": [{"type": "text", "text": "[3] entry \"Card number\" value=4000056655665556"}, {"type": "image", "data": "abc"},
                       {"type": "resource", "resource": {"text": "card 5555 5555 5555 4444"}}],
           "structuredContent": {"tree": [{"name": "Card number", "value": "4000 0566 5566 5556"}], "4242424242424242": 1,
                                 "deep": [[[[[[[[[[{"v": 4242424242424242}]]]]]]]]]]}}
    n1, n2, n3 = cardmask.mask_message(msg), cardmask.mask_message(note), cardmask.mask_result(res)
    check("F6: every message the driver sends is masked whole: an error's message, a numeric value, a progress note, embedded "
          "resource text, keys, any depth", n1 == 2 and M in msg["error"]["message"] and msg["error"]["data"]["n"] == M and n2 == 1
          and M in note["params"]["message"] and n3 == 5 and res["structuredContent"]["deep"][0][0][0][0][0][0][0][0][0][0]["v"] == M
          and M in res["content"][2]["resource"]["text"] and M in res["structuredContent"], (msg, note, res))
    try:
        old = cardmask.MAX_NODES
        cardmask.MAX_NODES = 50
        cardmask.mask_value(list(range(100)))
        unfinished = False
    except cardmask.Unfinished:
        unfinished = True
    finally:
        cardmask.MAX_NODES = old
    check("F6: a message with more values than the check takes raises Unfinished (the hub then withholds it), never passes unchecked", unfinished)
    salt = "a1b2"
    hint = {"exp": True, "cvc_len": 3, "zip_shape": "AA9 9A9",
            "name": {"salt": salt, "sha256": hashlib.sha256((salt + HOLDER.lower()).encode()).hexdigest(), "words": 3}}
    page = f"entry \"Expiry\" 11/30 entry \"CVC\" 123 entry \"Name\" {HOLDER} entry \"ZIP\" {ZIP} price $12.34 qty 2"
    masked, n = cardmask.mask(page, [hint])
    check("F6: while a purchase is open, its expiry, security code, postal code (by shape) and name (by fingerprint) are masked "
          "too; prices and small numbers aren't", n == 4 and "11/30" not in masked and " 123 " not in masked and HOLDER not in masked
          and ZIP not in masked and "$12.34" in masked and "qty 2" in masked, masked)
    rows = "[10] entry \"Card 1\" 4242\n[11] entry \"Card 2\" 4242\n[12] entry \"Card 3\" 4242\n[13] entry \"Card 4\" 4242"
    check("F6: while a purchase is open, a number split across four fields in four rows is masked", "4242" not in cardmask.mask(rows, [{}])[0],
          cardmask.mask(rows, [{}])[0])
    # F6 (the re-check's cases): structured forms of a card's parts.
    jx = "J. Example"
    act = [{"exp": True, "cvc_len": 3, "zip_shape": "99999",
            "name": {"salt": salt, "sha256": hashlib.sha256((salt + "j example").encode()).hexdigest(), "words": 2}}]
    forms = {"float": {"n": 4242424242424242.0}, "split": {"n": ["4242", "4242", "4242", "4242"]}, "split-ints": {"n": [4242, 4242, 4242, 4242]},
             "numeric cvc and zip": {"cvc": 123, "zip": 94107}, "expiry fields": {"expiry_month": "11", "expiry_year": "2030"},
             "name with an initial": {"holder": jx}, "an accessibility node": {"role": "entry", "name": "Security code", "value": "123"},
             "a node's text": {"t": '[12] entry "Security code" value "123"'}}
    left = {}
    for what, v in forms.items():
        out, _ = cardmask.mask_value(json.loads(json.dumps(v)), act)
        s_ = json.dumps(out)
        if any(x in s_ for x in ("4242", "123", "94107", '"11"', "2030", jx)):
            left[what] = s_
    msg2 = {"jsonrpc": "2.0", "id": 9, "4242 4242 4242 4242": "x", "result": {}}
    cardmask.mask_message(msg2, act)
    check("F6: structured forms are masked too: a float, a number split across a list's strings or numbers, a numeric security code "
          "and postal code, separate expiry month and year, a name with an initial, an accessibility node's value, and a top-level key",
          not left and "4242 4242 4242 4242" not in msg2, (left, msg2))
    import base64 as b64
    png = b64.b64encode(b"\x89PNG" + b"\x00" * 40 + b"123 4242424242424242 11/30 94107").decode()
    ordinary = {"url": "https://shop.example:443/checkout/123", "ref": '[123] button "Place order"', "word": "abc123xyz",
                "hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "pid": 45663, "price": "Total $12.34",
                "years": ["2021", "2022", "2023", "2024"], "path": "/a/12/25/b",
                "content": [{"type": "image", "data": png, "mimeType": "image/png"}],
                "resource": {"uri": "file:///x.png", "mimeType": "image/png", "blob": png}, "shot": "data:image/png;base64," + png}
    out, n = cardmask.mask_value(json.loads(json.dumps(ordinary)), act)
    changed = [k for k in ordinary if json.dumps(ordinary[k]) != json.dumps(out[k])]
    check("N3: while a purchase is open, ordinary results aren't changed: a URL's port and path, an element reference, a word with digits, "
          "a hash, a process id, a price, four years in a row, a path with dates, and encoded images (an image's data, a resource's blob, "
          "a data: URL) keep every byte", not changed, (changed, n))
    tres = {"type": "resource", "resource": {"uri": "file:///o.txt", "mimeType": "text/plain",
                                             "blob": b64.b64encode(b"card 4242 4242 4242 4242").decode()}}
    cardmask.mask_value(tres, [])
    check("N3: a resource's text blob is decoded, masked and encoded again (still valid base64)",
          "4242" not in b64.b64decode(tres["resource"]["blob"]).decode(), tres)
    dir_v = os.path.join(td, "vmvault")
    os.makedirs(dir_v, exist_ok=True)
    open(os.path.join(dir_v, "purchases-active.json"), "w").write(json.dumps({"v": 1, "purchases": [{"vm": "other", "until": time.time() + 600, "cvc_len": 3, "exp": True}]}))
    import mcp_hub
    hubs = {}
    for vm_ in ("mine", "other"):
        e_ = dict(os.environ, MCOMP_RUN_DIR=os.path.join(td, "hubrun-" + vm_), MCOMP_LOG_DIR=os.path.join(td, "hublog-" + vm_),
                  MCOMP_VAULT_DIR=dir_v, MCOMP_VM_NAME=vm_)
        os.makedirs(e_["MCOMP_RUN_DIR"], exist_ok=True)
        hubs[vm_] = mcp_hub.Hub(env=e_).card_hints()
    check("N3: a purchase open on another VM sharing the vault doesn't change this VM's results (the hub's masks are its VM's)",
          hubs["mine"] == [] and len(hubs["other"]) == 1, hubs)
    # R2: while a purchase is open in that VM, ordinary text is masked aggressively; outside one, precisely.
    act2 = [{"exp": True, "cvc_len": 3, "zip_shape": "99999"}]
    plain = {"CVC:123": "123", "Security code=123": "123", "Card number4242424242424242": "4242424242424242",
             "4242424242424242Visa": "4242424242424242", "Ref 1111 1111 1111 1111": "1111 1111 1111 1111"}
    still = {t: cardmask.mask(t, act2)[0] for t in plain if plain[t] in cardmask.mask(t, act2)[0]}
    blob = {"type": "resource", "resource": {"uri": "x.json", "mimeType": "application/json", "blob": b64.b64encode(
        json.dumps({"cvv": 123, "zip": 94107, "expiry_month": "11", "expiry_year": "2030"}).encode()).decode()}}
    cardmask.mask_value(blob, act2)
    dec = b64.b64decode(blob["resource"]["blob"]).decode()
    jtext = {"text": json.dumps({"cvv": 123, "zip": 94107})}
    cardmask.mask_value(jtext, act2)
    check("R2: while a purchase is open: a code after \"CVC:\" or \"Security code=\", a number run into words, any run of 12 or "
          "more digits (even failing the card check), and a JSON blob's or JSON text's named fields are masked",
          not still and not any(x in dec for x in ("123", "94107", '"11"', "2030")) and "123" not in jtext["text"] and "94107" not in jtext["text"],
          (still, dec, jtext))
    check("R2: outside a purchase the masks stay precise: a number that fails the card check, or one run into words, is left as it is",
          cardmask.mask("Ref 1111 1111 1111 1111")[0] == "Ref 1111 1111 1111 1111" and cardmask.mask("Card number4242424242424242")[0] == "Card number4242424242424242")
    n1_log_paths(td, cardmask, M)



# Where an action log's records hold no text of the driver's: the chain's hashes and signature, keyed fingerprints, the
# signing key's name, times and the file's own name. They're random or made from the clock, so they hold any digits
# ("4242", "123") now and then: the checks below look for the driver's text in the rest only.
ACTION_LOG_DIGEST_KEYS = {"prev", "hash", "sig", "head", "signing_key", "hmac", "ts", "file"}


def action_log_text(paths):
    """Every string an action log's records keep (a result's summary, kept as JSON text, read as JSON), but for the
    digests and names in ACTION_LOG_DIGEST_KEYS, one per line."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k not in ACTION_LOG_DIGEST_KEYS:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            try:
                inner = json.loads(v)
            except ValueError:
                inner = None
            if isinstance(inner, (dict, list)):
                walk(inner)
            else:
                out.append(v)
    for path in paths:
        for line in open(path):
            if line.strip():
                walk(json.loads(line))
    return "\n".join(out)


def n1_log_paths(td, cardmask, M):
    """N1: every path that keeps or answers with what the driver sent masks it first: the action log (its call, result and
    error records), the driver's stderr (kept in memory and written to the hub's log), and the error a failed connection
    answers with."""
    import actionlog
    import wirelimits
    cfg = {"keep_text": True, "key": os.path.join(td, "alkey", "id_ed25519"), "dir": os.path.join(td, "actions")}
    os.makedirs(os.path.dirname(cfg["key"]), exist_ok=True)
    log = actionlog.ActionLog({"bridge_pid": 1}, cfg=cfg)
    log.call("c1", "type_text", {"text": "4242 4242 4242 4242"})
    log.result("c1", "type_text", {"error": {"message": "the field holds 4242424242424242"}})
    log.call("c2", "get_window_state", {})
    log.result("c2", "get_window_state", {"result": {"content": [{"type": "text", "text": "value 4242-4242-4242-4242"}]}})
    log.close()
    body = action_log_text([log.path])
    check("N1: the action log keeps a call's arguments, a result's text and an error's message only masked (ACTION_LOG_TEXT=keep too)",
          "4242" not in body and M in body, body[-600:])
    def counter(path_):
        r_, w_ = os.pipe()
        try:
            return wirelimits.StderrCounter(os.fdopen(r_, "rb"), path_), w_
        except AttributeError:                    # an older wirelimits: the hub's tail of the text (the checks then fail)
            return wirelimits.StderrTail(os.fdopen(r_, "rb"), path_), w_
    path = os.path.join(td, "stderr.log")
    tail, w = counter(path)
    os.write(w, b"driver: page said 4242 4242 4242 4242\nand 5555")
    os.write(w, b"555555554444 at the end")
    os.close(w)
    tail.done.wait(5)
    kept, written = tail.text(wait=1.0), open(path).read()
    check("N1: none of the driver's stderr is kept or written to the hub's log (a number split across two reads too): only counts "
          "and fixed classes", "4242" not in kept + written and "5555" not in kept + written and "page said" not in kept + written
          and "2 line(s)" in kept, (kept, written))
    path2 = os.path.join(td, "stderr2.log")
    tail2, w = counter(path2)
    big = 16384
    os.write(w, b"x" * (big - 9) + b" 42424242")
    time.sleep(0.3)
    os.write(w, b"42424242 then the rest of the line\n")
    os.close(w)
    tail2.done.wait(5)
    w2, k2 = open(path2).read(), tail2.text(wait=1.0)
    check("R3: a card number in a stderr line too long to wait for (8 digits before the old cut, 8 after) is never let out, in parts "
          "or whole", "4242" not in w2 + k2, w2[-200:])
    # R14: the hub keeps none of the driver's stderr: counts and fixed classes only, purchase or not.
    path3 = os.path.join(td, "stderr3.log")
    ctr, w = counter(path3)
    os.write(w, b"CVC:123 ZIP:94107 expiry 11/30")       # an unfinished line...
    time.sleep(0.3)
    os.write(w, b"\nerror: the page said 4242 4242 4242 4242\nwarning: slow\n" + b"x" * 40000 + b"\n")
    os.close(w)
    ctr.done.wait(5)
    w3, t3 = open(path3).read(), ctr.text(wait=1.0)
    cls3 = ctr.classes() if hasattr(ctr, "classes") else {}
    check("R14: the hub keeps none of the driver's stderr, purchase or not: a line's text never reaches the log or a message, only "
          "how many lines there were and fixed classes (driver error, driver warning, long line ...), an unfinished line included",
          not any(x in w3 + t3 for x in ("CVC:", "94107", "11/30", "4242", "slow", "page said")) and cls3.get("driver error") == 1
          and cls3.get("driver warning") == 1 and cls3.get("long line") == 1 and "4 line(s)" in t3, (w3[-300:], t3))
    # R15: the action log's purchase state is read fresh for every record: a purchase that opened a moment ago counts.
    fresh = os.path.join(td, "fresh-vault")
    os.makedirs(fresh, exist_ok=True)
    saved_env = {k: os.environ.get(k) for k in ("MCOMP_VAULT_DIR", "MCOMP_VM_NAME")}
    os.environ.update({"MCOMP_VAULT_DIR": fresh, "MCOMP_VM_NAME": "pvm"})
    try:
        actionlog.mask_record({"type": "result", "rpc_error": "nothing yet"})          # no purchase open yet
        open(os.path.join(fresh, "purchases-active.json"), "w").write(json.dumps({"v": 1, "purchases": [
            {"vm": "pvm", "until": time.time() + 600, "cvc_len": 3, "exp": True, "zip_shape": "99999", "ack": "a1"}]}))
        rec = actionlog.mask_record({"type": "result", "rpc_error": "CVC:123 ZIP:94107 expiry 11/30"})
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("R15: the action log reads the open purchases fresh for every record (no cached \"no purchase\"): a purchase opened a moment "
          "after the last record still masks the next one", not any(x in json.dumps(rec) for x in ("123", "94107", "11/30")), rec)
    import mcp_hub
    # R31: masks are sticky. Once read, a purchase's masks stay for the hub's (and the action-log relay's) life, whatever
    # the file's window or the clock says later; an error's message in the action log is masked with them.
    sv = os.path.join(td, "sticky-vault")
    os.makedirs(sv, exist_ok=True)
    active = os.path.join(sv, "purchases-active.json")
    open(active, "w").write(json.dumps({"v": 1, "purchases": [
        {"vm": "svm", "until": time.time() + 60, "cvc_len": 3, "exp": True, "zip_shape": "99999", "ack": "s1"}]}))
    henv_s = dict(os.environ, MCOMP_RUN_DIR=os.path.join(td, "hubrun-s"), MCOMP_LOG_DIR=os.path.join(td, "hublog-s"),
                  MCOMP_VAULT_DIR=sv, MCOMP_VM_NAME="svm", VM_NAME="svm")
    os.makedirs(henv_s["MCOMP_RUN_DIR"], exist_ok=True)
    hub_s = mcp_hub.Hub(env=henv_s)
    hub_s.card_hints()                                  # the hub reads the purchase (as for any result)
    saved_env = {k: os.environ.get(k) for k in ("MCOMP_VAULT_DIR", "MCOMP_VM_NAME", "MCOMP_ACTION_LOG_TEXT")}
    os.environ.update({"MCOMP_VAULT_DIR": sv, "MCOMP_VM_NAME": "svm"})
    cfg_s = dict(cfg, keep_text=False, dir=os.path.join(td, "actions-s"))
    log_s = actionlog.ActionLog({"bridge_pid": 1}, cfg=cfg_s)
    log_s.call("s1", "get_window_state", {})
    actionlog._card_hints()                             # ...and so does the relay

    class _Ahead:                                       # the clock a week on (a jump, a sleep, a long wait)
        def __init__(self, real):
            self.real = real

        def __getattr__(self, n):
            return getattr(self.real, n)

        def time(self):
            return self.real.time() + 7 * 86400
    real_time = cardmask.time
    cardmask.time = _Ahead(real_time)
    try:
        open(active, "w").write(json.dumps({"v": 1, "purchases": [                  # and the file's window has passed
            {"vm": "svm", "until": time.time() - 5, "cvc_len": 3, "exp": True, "zip_shape": "99999", "ack": "s1"}]}))
        hub_text = cardmask.mask_text("CVC: 123, expiry 11/30, ZIP 94107", hub_s.card_hints())
        log_s.result("s1", "get_window_state", {"error": {"message": "the field says CVC: 123, expiry 11/30"}})
        log_s.close()
    finally:
        cardmask.time = real_time
        hub_s.shutdown()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    files_s = [os.path.join(dp, f) for dp, _, fs in os.walk(cfg_s["dir"]) for f in fs if f.endswith(".jsonl")]
    body_s = "".join(open(f).read() for f in files_s)
    text_s = action_log_text(files_s)
    check("R31: once read, a purchase's masks stay for the hub's and the action log's life: with the clock a week on and the "
          "file's window past, the hub still masks the security code, expiry and postal code, and so does an error's "
          "message in the action log (text mode redact)", not any(x in hub_text for x in ("123", "11/30", "94107"))
          and "rpc_error" in body_s and not any(x in text_s for x in ("123", "11/30")), (hub_text, text_s[-400:]))
    # R36: every purchase still in the file keeps its masks, however many (the bound is for those no longer in it), and
    # the hub acknowledges only what it holds.
    mv = os.path.join(td, "many-vault")
    os.makedirs(mv, exist_ok=True)

    def fp36(name):
        salt = os.urandom(8).hex()
        norm = " ".join(name.lower().split())
        return {"salt": salt, "sha256": hashlib.sha256((salt + norm).encode()).hexdigest(), "words": len(norm.split())}
    many = [{"vm": "mvm", "until": time.time() + 600, "cvc_len": 4, "exp": True, "ack": "m0", "name": fp36("Alice Example")}]
    many += [{"vm": "mvm", "until": time.time() + 600, "cvc_len": 3, "exp": True, "ack": f"m{i}",
              "name": fp36("Person " + chr(97 + i % 26) + chr(97 + i // 26))} for i in range(1, 70)]
    open(os.path.join(mv, "purchases-active.json"), "w").write(json.dumps({"v": 1, "purchases": many}))
    hub_m = mcp_hub.Hub(env=dict(os.environ, MCOMP_RUN_DIR=os.path.join(td, "hubrun-m"), MCOMP_LOG_DIR=os.path.join(td, "hublog-m"),
                                 MCOMP_VAULT_DIR=mv, MCOMP_VM_NAME="mvm", VM_NAME="mvm"))
    os.makedirs(os.path.join(td, "hubrun-m"), exist_ok=True)
    try:
        fresh36 = []
        try:
            held36 = hub_m.card_hints(fresh36)
        except TypeError:                                # an older hub: no list of what it acknowledges
            held36 = hub_m.card_hints()
            fresh36 = list(many)
        text36 = cardmask.mask_text("Alice Example; CVC: 1234", held36)
        # R40: then a read that fails (the file half-written, say): it can't tell what's gone, so nothing is let go.
        open(os.path.join(mv, "purchases-active.json"), "w").write("{not json")
        held40 = hub_m.card_hints()
        text40 = cardmask.mask_text("Alice Example; CVC: 1234", held40)
    finally:
        hub_m.shutdown()
    acked36 = {h.get("ack") for h in fresh36}
    check("R36: with 70 purchases in the file, every one keeps its masks (the oldest's name and 4-digit code stay hidden), and "
          "what the hub acknowledges is what it holds", "Alice" not in text36 and "1234" not in text36 and len(held36) >= 70
          and "m0" in acked36 and all(any(cardmask.hint_key(x) == cardmask.hint_key(h) for x in held36) for h in fresh36)
          if hasattr(cardmask, "hint_key") else False, (text36, len(held36), len(acked36)))
    check("R40: a read of the file that fails lets none of the 70 held masks go (the oldest's name still hidden)",
          "Alice" not in text40 and len([h for h in held40 if not h.get("unreadable")]) >= 70, (text40, len(held40)))
    drv = os.path.join(td, "bad-driver.sh")
    open(drv, "w").write("#!/bin/sh\necho 'cannot reach the page showing 4242 4242 4242 4242' >&2\nexit 3\n")
    os.chmod(drv, 0o755)
    henv = dict(os.environ, MCOMP_RUN_DIR=os.path.join(td, "hubrun2"), MCOMP_LOG_DIR=os.path.join(td, "hublog2"),
                MCOMP_VAULT_DIR=os.path.join(td, "nohubvault"), MCOMP_HUB_DRIVER_CMD=json.dumps([drv]))
    os.makedirs(henv["MCOMP_RUN_DIR"], exist_ok=True)
    hub = mcp_hub.Hub(env=henv)
    try:
        err = hub.connect_upstream() if hasattr(hub, "connect_upstream") else hub.connect()
    except Exception as e:     # noqa: BLE001
        err = repr(e)
    logs = "".join(open(os.path.join(dp, f), errors="replace").read() for root in (henv["MCOMP_LOG_DIR"], henv["MCOMP_RUN_DIR"])
                   for dp, _, fs in os.walk(root) for f in fs if not f.endswith((".sock", ".lock")))
    check("N1, R14: a connection that fails answers with what the hub derived from the driver's stderr (a count, fixed classes), never "
          "its text, and the hub's log holds none of it either", isinstance(err, str) and "4242" not in err and "cannot reach" not in err
          and "line(s) to stderr" in err and "4242" not in logs and "cannot reach" not in logs, (err, logs[-300:]))
    # R22: a line shaped like one of metacomputer's own refusals carries nothing through either: the hub keeps which refusal
    # it was and says it in its own words, from what it pinned.
    drv2 = os.path.join(td, "forging-driver.sh")
    open(drv2, "w").write("#!/bin/sh\necho \"metacomputer: the settings now name the VM '4242424242424242', not '731' as confirmed "
                          "(MCOMP_EXPECT_VM); refusing\" >&2\nexit 3\n")
    os.chmod(drv2, 0o755)
    henv3 = dict(henv, MCOMP_RUN_DIR=os.path.join(td, "hubrun3"), MCOMP_LOG_DIR=os.path.join(td, "hublog3"),
                 MCOMP_HUB_DRIVER_CMD=json.dumps([drv2]), MCOMP_VM_NAME="pvm")
    os.makedirs(henv3["MCOMP_RUN_DIR"], exist_ok=True)
    hub3 = mcp_hub.Hub(env=henv3)
    try:
        err3 = hub3.connect_upstream() if hasattr(hub3, "connect_upstream") else hub3.connect()
    except Exception as e:     # noqa: BLE001
        err3 = repr(e)
    logs3 = "".join(open(os.path.join(dp, f), errors="replace").read() for root in (henv3["MCOMP_LOG_DIR"], henv3["MCOMP_RUN_DIR"])
                    for dp, _, fs in os.walk(root) for f in fs if not f.endswith((".sock", ".lock")))
    check("R22: a driver line shaped like metacomputer's own refusal, a card number and CVC in its fields, reaches neither the "
          "answer nor the hub's log: the hub says which refusal, in its own words and with the VM it pinned",
          isinstance(err3, str) and "4242" not in err3 + logs3 and "731" not in err3 + logs3
          and "now name another VM, not 'pvm'" in err3 and "hub stop" in err3, (err3, logs3[-300:]))


def offline_acks(td, cardmask, M):
    """R25: the hub's ack worker (mcp_hub.Hub._ack_purchases_run) keeps looking for the vault folder: one set up after the
    hub started still gets its acks."""
    import mcp_hub
    vd = os.path.join(td, "late-vault")
    lock_dir = os.path.join(td, "late-run", "hub")
    os.makedirs(lock_dir, mode=0o700)
    lock = os.path.join(lock_dir, "hub.pid")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    class Core:                                  # what the worker reads of the hub
        env = {"MCOMP_VAULT_DIR": vd, "MCOMP_VM_NAME": "latevm"}
        _ack_stop = threading.Event()
        ack_lock = lock
        if hasattr(cardmask, "StickyHints"):
            sticky_hints = cardmask.StickyHints()
        card_hints = mcp_hub.Hub.card_hints
    core = Core()
    t = threading.Thread(target=mcp_hub.Hub._ack_purchases_run, args=(core,), daemon=True)
    t.start()
    time.sleep(0.8)                              # the hub has started; the vault isn't set up yet
    os.makedirs(vd, mode=0o700)
    with open(os.path.join(vd, "purchases-active.json"), "w") as f:
        json.dump({"v": 1, "purchases": [{"vm": "latevm", "until": time.time() + 600, "cvc_len": 3, "ack": "late-ack"}]}, f)
    path, got = cardmask.ack_path(vd, "latevm"), ""
    for _ in range(60):
        try:
            got = open(path).read()
        except OSError:
            got = ""
        if "late-ack" in got:
            break
        time.sleep(0.1)
    core._ack_stop.set()
    t.join(2)
    os.close(fd)
    check("R25: a hub started before its vault was set up still says which purchases it has seen once the vault is there (its "
          "ack worker keeps looking for the vault folder)", "late-ack" in got, (t.is_alive(), got[:200]))


def offline_hub(td, cardmask, M):
    """F7: the hub masks a driver message before anything else sees it (its own observe, traces, notes), and every client."""
    import mcp_hub
    henv = dict(os.environ, MCOMP_RUN_DIR=os.path.join(td, "hubrun"), MCOMP_LOG_DIR=os.path.join(td, "hublog"),
                MCOMP_VAULT_DIR=os.path.join(td, "nohubvault"))
    os.makedirs(henv["MCOMP_RUN_DIR"], exist_ok=True)
    hub = mcp_hub.Hub(env=henv)
    seen, sent = [], []

    class Client:
        closed = False

        def send(self, m):
            sent.append(json.dumps(m))
    hub.observe = lambda pend, m, key: seen.append(json.dumps(m))
    hub.pending["k1"] = {"id": 41, "client": Client(), "tool": "get_window_state", "args": {}}
    hub.on_upstream({"jsonrpc": "2.0", "id": "k1", "result": {"content": [{"type": "text", "text": "value=4242 4242 4242 4242"}],
                                                               "structuredContent": {"name": "4242424242424242"}}})
    hub.progress[json.dumps("t1")] = (Client(), "t1")
    hub.on_upstream({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "t1", "message": "4242424242424242"}})
    check("F7: the hub masks a driver answer before its own observe (traces, notes, events) sees it, and what every client gets, "
          "progress notes included", seen and all(NUMBER not in x and "4242 4242" not in x for x in seen + sent) and len(sent) == 2
          and all(M in x for x in sent), (seen, sent))
    orig = cardmask.mask_message
    cardmask.mask_message = lambda m, a=(): (_ for _ in ()).throw(cardmask.Unfinished("test"))
    sent.clear()
    hub.pending["k2"] = {"id": 42, "client": Client(), "tool": "get_window_state", "args": {}}
    hub.on_upstream({"jsonrpc": "2.0", "id": "k2", "result": {"content": [{"type": "text", "text": "4242424242424242"}]}})
    cardmask.mask_message = orig
    check("F7: an answer the check can't finish isn't passed on: the client gets an error saying so", len(sent) == 1 and NUMBER not in sent[0]
          and "couldn't check this answer for card numbers" in sent[0], sent)



def offline_human():
    H = tvc.human_module()
    S, show, ed = tvc.S, H.SHOWING, H.EDITABLE
    page = [("heading", "Checkout", S(show), "Checkout", None, []), ("paragraph", "", S(show), "Subtotal $10.00", None, []),
            ("paragraph", "", S(show), "Sub-total $10.00", None, []),
            ("paragraph", "", S(show), "Total $10.80", None, []), ("paragraph", "", S(show), "Total savings $1.00", None, []),
            ("entry", "Card number", S(show, ed), "", None, [])]
    t = tvc.FakeTree("Firefox", [tvc.firefox("http://localhost:8000/checkout.html", "http://localhost:8000/checkout.html", "Checkout", page)])
    got = H.page_text(t, t.pid, None, ["http://localhost:8000"], "Total")
    o, texts, befores = tuple(got) + (None,) * (3 - len(got))           # an older helper gives no befores
    check("human mode: every place \"Total\" is (not \"Subtotal\") on the site the address bar names comes back for the vault to read, "
          "with the words before it (F12: so \"Sub-total\" isn't taken for the total)",
          o == "http://localhost:8000" and len(texts) == 3 and texts[1].startswith("Total $10.80") and texts[2].startswith("Total savings")
          and befores is not None and len(befores) == 3 and befores[0].endswith("Sub-"), (o, texts, befores))
    try:
        H.page_text(t, t.pid, None, ["https://shop.example"], "Total")
        ok = False
    except H.Refused as e:
        ok = "isn't allowed" in str(e)
    check("human mode: ...and refused for another site than the one the address bar shows", ok)
    t2 = tvc.FakeTree("Firefox", [tvc.firefox("https://shop.example/c", "https://evil.example/c", "Checkout", page)])
    try:
        H.page_text(t2, t2.pid, None, ["https://shop.example"], "Total")
        ok = False
    except H.Refused:
        ok = True
    check("human mode: a total isn't read when the address bar and the page's own address disagree", ok)
    check("human mode: a card part goes into an entry, a spin button or a password text (a security code field)",
          H.FIELD_ROLES["card"] == {"entry", "spin button", "password text"})

    class Driver:
        """Stands in for Cua Driver: typing changes the fake tree as the test says."""

        def __init__(self, on_type):
            self.on_type = on_type

        def call(self, method, params, timeout=20):
            if (params or {}).get("name") == "type_text":
                self.on_type(params["arguments"]["text"])
            return {}

        def close(self):
            pass

    def fill_case(after):
        foc = H.FOCUSED
        f1 = ("entry", "Card number", S(show, ed, foc), "", None, [])
        f2 = ("entry", "Expiry", S(show, ed), "", None, [])
        t = tvc.FakeTree("Firefox", [tvc.firefox("http://localhost:8000/checkout.html", "http://localhost:8000/checkout.html", "Checkout", [f1, f2])])
        card, other = t.find("entry", "Card number"), t.find("entry", "Expiry")

        def typed(v):
            t.counts[card] = len(v)
            after(t, card, other)
        req = {"mode": "fill", "field": "card", "origins": ["http://localhost:8000"], "value": "4242424242424242"}
        try:
            return H.run(req, tree=t, env={}, info=(1, t.pid, None, None), driver=Driver(typed))
        except (H.Refused, H.Failed) as e:
            return {"ok": False, "reason": str(e)}

    def popup(t, card, other):          # the browser's own autocomplete list appears: the field's place in the tree moves
        frame = t.find("frame")
        t.nodes[frame][5].insert(0, t._add(("panel", "Autocomplete", S(show), "", None, [])))

    def moved(t, card, other):          # the focus went to another field while typing
        r, n, st, *rest = t.nodes[card]
        t.nodes[card] = (r, n, st & ~(1 << H.FOCUSED), *rest)
        r, n, st, *rest = t.nodes[other]
        t.nodes[other] = (r, n, st | (1 << H.FOCUSED), *rest)
    a, b = fill_case(popup), fill_case(moved)
    check("human mode: a fill whose field moved in the tree (an autocomplete list appeared) but kept the focus counts; one whose "
          "focus went to another field doesn't", a.get("ok") is True and b.get("ok") is False and "focus moved" in b.get("reason", ""), (a, b))


def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    st = subprocess.run([BIN, "selftest"], capture_output=True, text=True)
    check("selftest: cards, page totals, agents.md, UCP profiles, RFC 9421 Ed25519, AP2 mandate form and refusals", st.returncode == 0
          and "PASS  page total 'Subtotal $10.00 Tax $0.80 Total $10.80'" in st.stdout and "PASS  agents.md: a ban on scripted checkout" in st.stdout
          and "PASS  AP2 closed mandates" in st.stdout and "PASS  RFC 9421 B.2.6" in st.stdout and "PASS  AP2 checkouts refused" in st.stdout,
          st.stdout[-600:])
    check("R19 (selftest): the proxy checks an IPv4 address written as IPv6 (mapped, NAT64, compatible) as IPv4, and refuses this "
          "Mac's own addresses in any form", "PASS  proxy addresses: an IPv4 address written as IPv6" in st.stdout, st.stdout[-300:])
    check("R26 (selftest): the proxy's shutdown never shuts a descriptor it no longer owns: a socket its worker closed while stop() "
          "ran, whose number another socket then took, is left working", "PASS  proxy shutdown: a socket a worker closed while stop() ran" in st.stdout,
          st.stdout[-300:])
    check("F8, F12 (selftest): an unsupported explicit currency, no currency, zero, two totals, \"Total savings\" after the total",
          "PASS  page total 'Total BHD 4.00'" in st.stdout and "PASS  page total 'Total $100.00 Total savings $1.00'" in st.stdout
          and "PASS  page total 'Total $0.00'" in st.stdout and "PASS  page total 'Total $5.00 ... Total $6.00'" in st.stdout
          and "PASS  page total 'Order total 12.34'" in st.stdout, st.stdout[-900:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []

    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    shopcli_dir, shopdata = os.path.join(td, "shopcli"), os.path.join(td, "shopdata")
    os.makedirs(shopcli_dir, mode=0o755)
    os.makedirs(shopdata)
    # The stand-in CLI is a package of its own (package.json beside it) in a folder an agent could write above: a module
    # planted in that folder's node_modules is what a confined run must never load.
    shop_pkg = os.path.join(shopcli_dir, "pkg")
    os.makedirs(os.path.join(shop_pkg, "bin"))
    open(os.path.join(shop_pkg, "package.json"), "w").write(json.dumps({"name": "@stand-in/shop", "version": "0.0.0", "type": "module"}))
    shop_cli = os.path.join(shop_pkg, "bin", "shop.mjs")
    open(shop_cli, "w").write(SHOP_STUB.replace("__DIR__", json.dumps(shopdata)).replace("__NUMBER__", json.dumps(SHOP_NUMBER))
                              .replace("__HOLDER__", json.dumps(SHOP_HOLDER)).replace("__ZIP__", json.dumps("SH0 P11")))
    os.chmod(shop_cli, 0o755)
    planted = os.path.join(shopcli_dir, "node_modules", "planted-helper")
    os.makedirs(planted)
    open(os.path.join(planted, "index.js"), "w").write(f"require('fs').writeFileSync({json.dumps(os.path.join(td, 'planted-ran'))}, 'yes');\n")
    # Node for the stand-in: a copy of this Mac's (its binary and libnode beside it, as Homebrew lays them out), so the
    # test can plant and change a library the runtime loads without touching the real one.
    node_src = find_node()
    runtime = node_lib = None
    if node_src:
        real = os.path.realpath(node_src)
        nroot = os.path.join(td, "node")
        os.makedirs(os.path.join(nroot, "bin"))
        os.makedirs(os.path.join(nroot, "lib"))
        runtime = os.path.join(nroot, "bin", "node")
        shutil.copy2(real, runtime)
        for lib in glob.glob(os.path.join(os.path.dirname(os.path.dirname(real)), "lib", "libnode*.dylib")):
            shutil.copy2(lib, os.path.join(nroot, "lib", os.path.basename(lib)))
            node_lib = os.path.join(nroot, "lib", os.path.basename(lib))
        for f in glob.glob(os.path.join(nroot, "*", "*")):
            os.chmod(f, 0o755)
    fake = os.path.join(td, "fake", "claude")
    os.makedirs(os.path.dirname(fake))
    open(fake + ".c", "w").write(tvo.FAKE_CLAUDE_C)
    rcf = subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True, text=True)
    proj = os.path.realpath(os.path.join(td, "proj"))
    os.makedirs(proj)

    # The stand-in merchant on this Mac's loopback: what its /agents.md and UCP profile say, set per check.
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Merchant)
    srv.got, srv.agents, srv.ucp = [], None, None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    procs.append(srv)
    SITE = f"http://127.0.0.1:{srv.server_address[1]}"
    SITE2 = "http://127.0.0.1:9"                    # nothing listens: rules can't be read
    PAYF = "https://pay.example"
    open(os.path.join(shopdata, "business-url"), "w").write(SITE + "/store")

    def sflag(name, content=""):
        open(os.path.join(stubdir, name), "w").write(content)

    def sunflag(name):
        p = os.path.join(stubdir, name)
        if os.path.exists(p):
            os.remove(p)

    def shopflag(name, on=True, content=""):
        p = os.path.join(shopdata, name)
        if on:
            open(p, "w").write(content)
        elif os.path.exists(p):
            os.remove(p)

    def shop_calls():
        p = os.path.join(shopdata, "calls.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_IGNORE_AGENT": "1", "MCOMP_VAULT_TEST_SSH": stub, "MCOMP_VAULT_TEST_FETCH_LOOPBACK": "1",
           "MCOMP_VAULT_TEST_LISTENERS": f"{my_name},cua-netgw", "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "20",
           "MCOMP_VAULT_TEST_SHOP_SPY": shopdata}
    alog = os.path.join(vdir, "audit.jsonl")

    def client(*args, extra=None, timeout=120, stdin=None):
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=dict(env, **(extra or {})),
                           input=stdin if stdin is not None else "", timeout=timeout)
        outputs.append(r.stdout + r.stderr)
        return r

    def bg(*args):
        p = subprocess.Popen([BIN, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
        bgs.append(p)
        return p

    def finish(p, timeout=120):
        o, e = p.communicate(timeout=timeout)
        outputs.append(o + e)
        return p.returncode, js(o), o + e

    def js(text):
        try:
            return json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError, AttributeError):
            return {}

    def tty(argv, answers, extra=None, timeout=90):
        """A command at a pseudo-terminal, answering its prompts; (exit status, what it showed), -1 when a prompt never came."""
        p = ts.Pty(argv, dict(env, **(extra or {})))
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        except AssertionError:
            rc = -1
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
        finally:
            p.close()
        outputs.append(p.text())
        return rc, p.text()

    def start_unlock(extra=None):
        p = ts.Pty([BIN, "unlock", "--for", "30m", *allow], dict(env, **(extra or {})))
        procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        tvc.wait_for(lambda: os.path.exists(os.path.join(vdir, "vault.sock")), 5, 0.05)
        return p

    def audit_recs():
        return [json.loads(l) for l in open(alog)] if os.path.exists(alog) else []

    def card_tty(name, extra_args, number=NUMBER, exp="11/30", cvc="123", holder=HOLDER, zip_=ZIP, upto=6):
        """`set NAME --kind card`, answering the first `upto` prompts (a refused part ends it there)."""
        return tty([BIN, "set", name, "--kind", "card", *extra_args],
                   [("Vault passphrase", PASS + "\n"), ("Card number for " + name, number + "\n"), ("Expiry (MM/YY)", exp + "\n"),
                    ("Security code (CVC)", cvc + "\n"), ("Name on the card", holder + "\n"), ("Billing ZIP or postal code", zip_ + "\n")][:upto])

    def spent(name):
        r = client("list", "--json")
        return {s["name"]: s for s in json.loads(r.stdout or "{}").get("secrets", [])}.get(name, {}).get("spent_in_period")

    bgs = []
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import cardmask as cardmask_lib
    acker_on, acker_stop = threading.Event(), threading.Event()
    acker_on.set()

    class section:
        """A part of the run: an exception in it is a failed check (with what raised), and the run goes on; a request left
        waiting for an approval is refused and ended, so the next part starts clean."""

        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                return False
            check(f"[{self.name}] ran to its end", False, repr(ev)[:400])
            for b in bgs:
                if b.poll() is None:
                    b.kill()
            try:
                if "u" in run_state:
                    run_state["u"].send("no\n")
            except OSError:
                pass
            return True
    run_state = {}

    # --- the vault and its cards --------------------------------------------------------------------------------------
    with section('the vault and its cards'):
        rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
        check("init", rc == 0, out[-300:])
        r = client("set", "visa", "--kind", "card")
        check("set --kind card without a terminal is refused (only the user adds a card, in their own terminal)",
              r.returncode != 0 and "own terminal" in r.stderr, r.stderr[-200:])
        rc, out = card_tty("visa", ["--label", "Test Visa"])
        check("set --kind card: number, expiry, security code, name and postal code typed hidden; it says every purchase asks, and "
              "recommends a virtual card", rc == 0 and "card \"Test Visa\"" in out and "every purchase asks you first" in out and "virtual card" in out
              and NUMBER not in out and HOLDER not in out, out[-500:])
        check("F1: a reusable card is saved with a plain warning: approval gates disclosure, not the charge",
              "approval gates disclosure, not the charge" in out and "can\r\ncharge it again" in out.replace("can \r\n", "can\r\n") or
              ("approval gates disclosure, not the charge" in out and "charge it again" in out), out[-700:])
        rc, out = card_tty("bad", [], number="4242424242424241", upto=2)
        check("a number that fails the card check is refused, and nothing is saved", rc != 0 and "isn't a card number" in out, out[-200:])
        rc, out = card_tty("old", [], exp="01/20", upto=3)
        check("an expiry in the past is refused", rc != 0 and "not in the past" in out, out[-200:])
        rc, out = card_tty("agentcard", ["--label", "Agent Card", "--pay", "agent", "--currency", "USD", "--max-purchase", "20", "--max-per", "30/1d",
                                         "--merchant-max", f"{SITE}=25/7d"], number=NUMBER2)
        check("an agent card with caps (20 a purchase, 30 a day, 25 a week at one store)", rc == 0 and "an agent card" in out
              and "USD 20.00 a purchase" in out and "USD 30.00 per day" in out and f"USD 25.00 per 7 days at {SITE}" in out, out[-500:])
        rc, out = card_tty("local", ["--label", "Only There", "--origin", SITE, "--merchant-locked"])
        check("F1: a card the user marks merchant-locked (to its --origin) is saved without the reusable-card warning",
              rc == 0 and f"(only at {SITE}), merchant-locked" in out and "approval gates disclosure" not in out, out[-400:])
        rc, out = tty([BIN, "set", "myshop", "--kind", "shop", "--label", "My Shop account", "--pay", "agent", "--max-purchase", "20"],
                      [("Vault passphrase", PASS + "\n")])
        check("a shop entry: the policy and caps for purchases through the user's Shop account (no value typed)",
              rc == 0 and "purchases through your Shop account" in out and "an agent card" in out, out[-300:])
        rc, out = tty([BIN, "rule", "add", "--secret", "visa", "--project", td, "--for", "1h", "--passphrase"], [("Vault passphrase", PASS + "\n")])
        check("no unattended rule covers a card's purchases (an agent card, with caps, is the way)", rc != 0 and "no rule covers purchases" in out, out[-300:])
        rc, out = tty([BIN, "bind", "pvm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user", "mcfill",
                       "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
        check("bind the stand-in VM", rc == 0, out[-300:])
        raw_vault = open(os.path.join(vdir, "vault.json")).read()
        check("the vault file holds no card part in the clear", NUMBER not in raw_vault and HOLDER not in raw_vault and ZIP not in raw_vault)

        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("pay while the vault is locked: exit 3, recorded", r.returncode == 3 and js(r.stdout).get("reason") == "locked", r.stdout[-200:])
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE)
        check("pay needs the total: --total-selector (or --human, whose total is read after its label)", r.returncode == 64 and "--total-selector" in r.stderr)
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE + "/checkout", "--total-selector", "#t")
        check("pay --origin is a site, not a page", r.returncode == 64)
        open(os.path.join(td, "shopify-agents.md"), "w").write(SHOPIFY_AGENTS)
        r = client("merchant-rules", "--file", os.path.join(td, "shopify-agents.md"), "--json")
        j = js(r.stdout)
        check("merchant-rules --file: a Shopify store's agents.md forbids scripted checkout and asks for the buyer (no unlock needed)",
              r.returncode == 0 and j.get("card_fill") == "forbidden" and j.get("forbids") == ["no-scripted-checkout"] and j.get("buyer_continues") is True
              and j.get("shopify") is True and "handoff --why payment" in j.get("next", ""), r.stdout[-400:])
        open(os.path.join(td, "buyer.md"), "w").write("# Agents\nWhen you reach checkout, ask the buyer to complete checkout themselves.\n")
        r = client("merchant-rules", "--file", os.path.join(td, "buyer.md"), "--json")
        check("F16: rules that only ask agents to have the buyer complete checkout mean no card fill (a handoff)",
              js(r.stdout).get("card_fill") == "forbidden", r.stdout[-300:])

        u = start_unlock()
        n0 = len(calls())
        # The hub's part for VM pvm (lib/mcp_hub.py, _ack_purchases_run): it says which open purchases it has seen, with the
        # same code the hub runs, holding a hub lock of its own (RUN_DIR/hub/hub.pid, as Server.run writes it); the vault
        # sends no card part until it has.
        hub_lock_dir = os.path.join(td, "pvm-run", "hub")
        os.makedirs(hub_lock_dir, mode=0o700, exist_ok=True)
        os.chmod(hub_lock_dir, 0o700)
        hub_lock = os.path.join(hub_lock_dir, "hub.pid")
        lock_fd = os.open(hub_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        my_start = cardmask_lib.process_start(os.getpid())[0] if hasattr(cardmask_lib, "process_start") else None
        os.write(lock_fd, json.dumps({"pid": os.getpid(), "start": my_start}).encode())
        run_state["hub_lock_fd"] = lock_fd                  # held for the whole run

        def acker():
            last = None
            while not acker_stop.is_set():
                if acker_on.is_set() and hasattr(cardmask_lib, "ack_purchases"):
                    try:
                        try:
                            last = cardmask_lib.ack_purchases(vdir, "pvm", last, lock=hub_lock)
                        except TypeError:                   # an older cardmask (no lock): as it was
                            last = cardmask_lib.ack_purchases(vdir, "pvm", last)
                    except Exception:
                        last = None
                acker_stop.wait(0.05)
        threading.Thread(target=acker, daemon=True).start()

    # --- 1. a purchase that asks: the question names the site, the page's total and the card's label -----------------
    with section('1. a purchase that asks: the question names the site, the pa'):
        srv.agents, srv.ucp = OPEN_AGENTS, {"payment": {"handlers": [{"id": "com.google.pay"}]}}
        sflag("read-text", "Subtotal $10.00 Tax $0.80 Total $12.34")
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc", "--fill", "exp=#exp",
               "--fill", "cvc=#cvc", "--fill", "name=#holder", "--fill", "zip=#zip")
        u.expect("Type visa to allow this one purchase", timeout=60)
        term = u.text()
        check("the unlock terminal asks for this purchase: the site, the page's total (as the page's own claim), the card's label "
              "and the merchant's rules, and no card part", "Pay USD 12.34 with Test Visa (card visa) on VM pvm?" in term and f"Card parts go to: {SITE}" in term
              and "the page's own claim" in term and "/agents.md published, nothing in it against card entry" in term
              and "A purchase: what you allow covers this one purchase." in term and not any(v in term for v in VALUES), term[-1200:])
        check("F1: the question itself says what approving means: a reusable card can be charged again by this site",
              "a reusable card can be charged again by this site" in term, term[-1200:])
        u.send("visa\n")
        rc, res, text = finish(p)
        check("after the typed approval the purchase fills number, expiry, security code, name and postal code into the tab on that site "
              "(exit 0); the output names the purchase and the parts, never a value", rc == 0 and res.get("ok") is True
              and res.get("filled") == ["number", "exp", "cvc", "name", "zip"] and res.get("total") == "USD 12.34"
              and re.fullmatch(r"[0-9a-f]{32}", res.get("purchase_id") or "") and not any(v in text for v in VALUES), text[-600:])
        new = calls()[n0:]
        fills = [c for c in new if c["mode"] == "fill"]
        check("the VM got each part once, each after the total was read again (a shown element) and a check without the value, only "
              "for that site, the tab itself held to it (top_origins)", [c["value"] for c in fills] == [NUMBER, "11/30", "123", HOLDER, ZIP]
              and all(c["origins"] == [SITE] and c["top_origins"] == [SITE] and c["field"] == "text" for c in fills)
              and len([c for c in new if c["mode"] == "check" and not c["has_value"]]) == 5 and new[0]["mode"] == "read" and new[0]["what"] == "total"
              and new[0]["selector"] == "#total", [(c["mode"], c["field"]) for c in new])
        recs = audit_recs()
        pays = [x for x in recs if x.get("op") in ("pay", "pay_fill", "authorize", "request") and x.get("use", "pay") == "pay"]
        pid1 = res.get("purchase_id")
        check("the audit log has the request, the approval, the purchase and each part (payment.request, payment.authorize, payment.fill), "
              "with the card's label and the site, the purchase by its short id (never its key), and no card part",
              any(x.get("event") == "payment.request" for x in pays) and any(x.get("event") == "payment.authorize" and x.get("approval") == "each:yes" for x in pays)
              and sum(1 for x in pays if x.get("event") == "payment.fill" and x.get("label") == "Test Visa" and x.get("origin") == SITE) == 5
              and any(x.get("op") == "pay" and x.get("result") == "ok" and x.get("total") == "USD 12.34" and x.get("purchase") == display_id(pid1) for x in pays)
              and (pid1 or "x") not in json.dumps(recs) and not any(v in json.dumps(recs) for v in VALUES), [x.get("event") for x in pays])
        r = client("pay", "--purchase", pid1, "--vm", "pvm", "--field", "number")
        check("each part goes in once per purchase: the number again is refused", r.returncode == 2 and "filled already" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        r = client("pay", "--purchase", display_id(pid1), "--vm", "pvm", "--field", "cvc")
        check("F14: the short id the audit log and terminal show isn't a capability: --purchase takes the 32-digit key pay printed",
              r.returncode == 64, (r.returncode, r.stderr[-200:]))
        if rcf.returncode == 0:
            r = subprocess.run([fake, proj, BIN, "pay", "--purchase", pid1, "--vm", "pvm", "--field", "cvc"], capture_output=True, text=True,
                               env=env, timeout=120)
            outputs.append(r.stdout + r.stderr)
            check("F14: a purchase belongs to the session that started it: another Claude Code session with its key is refused",
                  r.returncode == 2 and "another Claude Code session" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        r = client("pay", "--purchase", pid1, "--done")
        check("pay --done ends it: its record lists the parts filled", r.returncode == 0 and js(r.stdout).get("outcome") == "done"
              and any(x.get("op") == "pay_end" and x.get("purchase") == display_id(pid1) and x.get("filled") == ["number", "exp", "cvc", "name", "zip"]
                      and x.get("event") == "payment.end" for x in audit_recs()), r.stdout[-300:])
        left6 = json.load(open(os.path.join(vdir, "purchases-active.json"))).get("purchases", []) if os.path.exists(os.path.join(vdir, "purchases-active.json")) else []
        check("F6: the vault tells the hub what to mask while a purchase is open (shapes, a fingerprint, never a part), and once it "
              "ends only for the settle margin after its last fill (R27: a minute)", all(e.get("until", 0) <= time.time() + 62 for e in left6)
              and not any(v in json.dumps(left6) for v in VALUES), left6)

    # --- 2. refused: typed something else; nothing is sent ---------------------------------------------------------------
    with section('2. refused: typed something else; nothing is sent'):
        n1 = len(calls())
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc")
        u.expect("Type visa to allow this one purchase", timeout=60)
        u.send("no\n")
        rc, res, text = finish(p)
        check("a purchase the user doesn't allow: exit 2, nothing typed, recorded as denied (payment.denied)",
              rc == 2 and res.get("ok") is False and not any(c["has_value"] for c in calls()[n1:])
              and any(x.get("op") == "pay" and x.get("result") == "denied" and x.get("event") == "payment.denied" for x in audit_recs()), text[-300:])

    # --- 3. a purchase, part by part; a swapped origin; session and unlock answers don't widen it -------------------------
    with section('3. a purchase, part by part; a swapped origin; session and u'):
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        u.expect("Type visa to allow this one purchase", timeout=60)
        u.send("visa unlock\n")
        rc, res, text = finish(p)
        pid2 = res.get("purchase_id")
        active = json.load(open(os.path.join(vdir, "purchases-active.json"))) if os.path.exists(os.path.join(vdir, "purchases-active.json")) else {}
        hint = (active.get("purchases") or [{}])[0]
        check("`visa unlock` allows only this purchase (a purchase never becomes a standing grant)", rc == 0 and pid2
              and "a purchase: allowed for this one purchase only" in u.text(), text[-300:])
        check("F6: while it's open, the hub's file has the security code's length, the postal code's shape and a salted name fingerprint, "
              "never a part", hint.get("cvc_len") == 3 and hint.get("zip_shape") == "AA9 9A9" and hint.get("name", {}).get("words") == 3
              and not any(v in json.dumps(active) for v in VALUES) and stat.S_IMODE(os.stat(os.path.join(vdir, "purchases-active.json")).st_mode) == 0o600, active)
        sflag("read-text", "Subtotal $10.00 Tax $0.80 Total $99.00")
        n2 = len(calls())
        r = client("pay", "--purchase", pid2, "--vm", "pvm", "--field", "number")
        check("a page whose total went up after the approval (USD 12.34 -> 99.00) gets nothing more: refused, nothing typed",
              r.returncode == 2 and "over the USD 12.34 you approved" in js(r.stdout).get("reason", "") and not any(c["has_value"] for c in calls()[n2:]),
              r.stdout[-300:])
        sflag("read-text", "Subtotal $10.00 Tax $0.80 Total $12.34")
        n2 = len(calls())
        sflag("swap-next")
        r = client("pay", "--purchase", pid2, "--vm", "pvm", "--field", "number")
        check("a tab that moved to another site after the approval: refused at the check, nothing typed (a swapped origin)",
              r.returncode == 2 and "isn't on" in js(r.stdout).get("reason", "") and not any(c["has_value"] for c in calls()[n2:]), r.stdout[-300:])
        r = client("pay", "--purchase", pid2, "--vm", "pvm", "--field", "number", "--selector", "#cc")
        check("...and once it's back on the site, the part is filled (the tries are counted per part)", r.returncode == 0
              and js(r.stdout).get("filled") == ["number"], r.stdout[-300:])
        r = client("pay", "--purchase", pid2, "--vm", "other", "--field", "cvc")
        check("a purchase is for its VM only", r.returncode == 2 and "is for VM pvm" in js(r.stdout).get("reason", ""), r.stdout[-200:])
        r = client("pay", "--purchase", pid2, "--vm", "pvm", "--field", "cvc", "--human")
        check("a page-mode purchase can't fill through the human-mode route", r.returncode == 2 and "page-mode Chrome" in js(r.stdout).get("reason", ""))
        r = client("pay", "--purchase", "deadbeef" * 4, "--vm", "pvm", "--field", "cvc")
        check("a key that isn't an open purchase is refused", r.returncode == 2 and "no such purchase" in js(r.stdout).get("reason", ""))
        r = client("fill", "visa", "--vm", "pvm", "--field", "text")
        check("a card can't be filled with `fill` (only a purchase sends it)", r.returncode == 2 and "secrets pay visa" in js(r.stdout).get("reason", ""))
        # F15: a policy change ends purchases approved under the old one.
        rc, out = tty([BIN, "policy", "visa", "--currency", "USD", "--max-purchase", "5"], [("Vault passphrase", PASS + "\n")])
        n3 = len(calls())
        r = client("pay", "--purchase", pid2, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        check("F15: tightening a card's policy (a USD 5 cap) ends a purchase approved before it: its next part is refused, nothing typed",
              rc == 0 and r.returncode == 2 and "changed after the purchase was approved" in js(r.stdout).get("reason", "")
              and not any(c["has_value"] for c in calls()[n3:]), (out[-200:], r.stdout[-300:]))
        rc, out = tty([BIN, "policy", "visa", "--no-caps"], [("Vault passphrase", PASS + "\n")])

    # --- 4. the merchant's rules: a ban, buyer-only checkout, or rules that can't be read whole ------------------------------
    with section("4. the merchant's rules: a ban, buyer-only checkout, or rule"):
        srv.agents = SHOPIFY_AGENTS
        n4, t4 = len(calls()), len(u.text())
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc")
        j = js(r.stdout)
        check("a merchant whose /agents.md forbids scripted checkout (Shopify's words): refused (exit 2), nothing asked, nothing read "
              "from the page, and the agent is told to hand payment to the user", r.returncode == 2 and "forbids scripted checkout" in j.get("reason", "")
              and "metacomputer handoff --why payment" in j.get("handoff", "") and len(calls()) == n4 and "Type visa" not in u.text()[t4:]
              and (j.get("rules") or {}).get("shopify") is True, r.stdout[-500:])
        srv.agents = "# Agents\nPlease ask the buyer to complete checkout on our page.\n"
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F16: rules that ask for the buyer to complete checkout: refused, the agent hands payment over", r.returncode == 2
              and "buyer completing checkout" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        srv.agents = "# Agents\nYou may browse.\n" + "x" * (300 << 10) + "\nDo not script checkout forms.\n"
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F16: an /agents.md larger than the vault reads whole is \"can't tell\", never permission: an agent card refuses", r.returncode == 2
              and "can't tell" in js(r.stdout).get("reason", "") and "larger than this reads whole" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        srv.agents = "<!doctype html><html><body>Our shop</body></html>"
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F16: a web page where /agents.md should be is \"can't tell\" too: an agent card refuses", r.returncode == 2
              and "can't tell" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        srv.agents = "# Agents\nYou may browse and buy.\n"
        srv.agents_ctype = "text/html; charset=utf-8"
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        srv.agents_ctype = None
        check("F16: an answer served as a web page (Content-Type text/html), whatever its words: \"can't tell\", an agent card refuses",
              r.returncode == 2 and "can't tell" in js(r.stdout).get("reason", "") and "text/html" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        srv.agents = "# Agents\nYou may browse and buy.\n" + "\n" * 6000 + "<html><body><script>var x = 1;</script></body></html>"
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F16: a web page past the first 4 KiB of the answer (the whole body is looked at): \"can't tell\", an agent card refuses",
              r.returncode == 2 and "can't tell" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        srv.agents = b"\xff\xfe\x00rules"
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        u.expect("Type visa to allow this one purchase", timeout=60)
        t16 = u.text()
        rq16 = json.loads(client("requests", "--json").stdout or "{}").get("requests", [])
        u.send("no\n")
        finish(p)
        check("F16: an ask card at a store whose rules can't be read: the question says \"can't tell\"", "Merchant's agent rules: can't tell (its /agents.md isn't text)" in t16,
              t16[-600:])
        check("F16, F1: ...and so does what Touch ID's dialog and a passphrase approval show and sign (the request's summary), with what "
              "approving means", any("Store's agent rules: can't tell" in q.get("summary", "") and "can be charged again" in q.get("summary", "") for q in rq16), rq16)
        srv.agents = OPEN_AGENTS
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE2, "--total-selector", "#total")
        check("an agent card where the rules can't be read (no answer): refused (it fails closed; nobody is asked)", r.returncode == 2
              and "can't tell" in js(r.stdout).get("reason", ""), r.stdout[-300:])

    # --- 5. an agent card: no question within its caps; the caps hold; the spending record ------------------------------------
    with section('5. an agent card: no question within its caps; the caps hold'):
        sflag("read-text", "Total $12.00")
        t5 = len(u.text())
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc")
        j = js(r.stdout)
        check("an agent card pays within its caps without asking (approval policy:agent, recorded as payment.authorize)",
              r.returncode == 0 and j.get("approval") == "policy:agent" and j.get("filled") == ["number"] and "Type agentcard" not in u.text()[t5:]
              and any(x.get("op") == "authorize" and x.get("approval") == "policy:agent" and x.get("event") == "payment.authorize" for x in audit_recs()),
              r.stdout[-400:])
        client("pay", "--purchase", j.get("purchase_id") or "0" * 32, "--done")
        sflag("read-text", "Total $21.00")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("over its cap a purchase (USD 21.00 > 20.00): refused before anything is asked", r.returncode == 2 and "a purchase" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        sflag("read-text", "Total $13.50")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("over its cap at this store (12.00 spent + 13.50 > 25.00 a week): refused", r.returncode == 2
              and f"at {SITE}" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        sflag("read-text", "Total $9.00")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        held = js(r.stdout).get("purchase_id")
        sflag("read-text", "Total €5.00")
        r2 = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("a total in another currency than the caps': refused (the caps can't be checked)", r2.returncode == 2 and "EUR" in js(r2.stdout).get("reason", ""),
              r2.stdout[-300:])
        sflag("read-text", "Total BHD 4.00")
        r2 = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F8: a total in a currency the vault doesn't support (BHD) is refused, never read as the card's USD", r2.returncode == 2
              and "BHD" in js(r2.stdout).get("reason", "") and "USD 4.00" not in r2.stdout, r2.stdout[-300:])
        sflag("read-text", "Total 4.00")
        r2 = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F8: a total with no currency at all isn't given the card's: a card with caps refuses it", r2.returncode == 2
              and "no currency" in js(r2.stdout).get("reason", ""), r2.stdout[-300:])
        sflag("read-text", "Total $4.50")
        r3 = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("an open purchase holds its amount against the caps (12.00 + 9.00 open + 4.50 > 25.00 a week here)",
              r.returncode == 0 and held and r3.returncode == 2 and "used or held" in js(r3.stdout).get("reason", ""), (r.stdout[-200:], r3.stdout[-300:]))
        # F2: a fill that took the value and then reported a failure still counts.
        sflag("fail-after-value-next")
        before = spent("agentcard")
        r = client("pay", "--purchase", held or "0" * 32, "--vm", "pvm", "--field", "number", "--selector", "#cc")
        client("pay", "--purchase", held or "0" * 32, "--done")
        check("F2: a part the VM took and then reported as failed still counts: the purchase stays in the spending record after it ends",
              r.returncode != 0 and before == "USD 12.00" and spent("agentcard") == "USD 21.00", (r.stdout[-200:], before, spent("agentcard")))
        sflag("read-text", "Total $3.00")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        pid_w = js(r.stdout).get("purchase_id")
        vj2 = os.path.join(vdir, "vault.json")
        subprocess.run(["chflags", "uchg", vj2])  # the vault file can't be replaced (the folder stays writable: the hub's files)
        n5 = len(calls())
        try:
            r = client("pay", "--purchase", pid_w or "0" * 32, "--vm", "pvm", "--field", "number", "--selector", "#cc")
        finally:
            subprocess.run(["chflags", "nouchg", vj2])
        check("F2: when the spending record can't be written, the part isn't sent (refused, nothing reached the VM's fill)",
              r.returncode == 2 and "couldn't be recorded" in js(r.stdout).get("reason", "") and not any(c["has_value"] for c in calls()[n5:]), r.stdout[-300:])
        client("pay", "--purchase", pid_w or "0" * 32, "--done")
        r = client("list", "--json")
        lst = {s["name"]: s for s in json.loads(r.stdout or "{}").get("secrets", [])}
        check("list shows a card's label, policy and caps, what it spent in the period, and never a part of it (not even the last digits)",
              lst.get("agentcard", {}).get("label") == "Agent Card" and lst["agentcard"].get("pay") == "agent"
              and lst.get("visa", {}).get("fields") == ["card"] and "4242" not in r.stdout and "5556" not in r.stdout, r.stdout[-600:])

    # --- 6. payment frames and merchant limits -----------------------------------------------------------------------------
    with section('6. payment frames and merchant limits'):
        r = client("pay", "local", "--vm", "pvm", "--origin", "https://elsewhere.example", "--total-selector", "#t")
        check("a card limited to one merchant is refused elsewhere", r.returncode == 2 and "pays only at" in js(r.stdout).get("reason", ""))
        sflag("read-text", "Total $12.34")
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--frame-origin", PAYF)
        u.expect("Type visa to allow this one purchase", timeout=60)
        term = u.text()
        rq = json.loads(client("requests", "--json").stdout or "{}").get("requests", [])
        u.send("visa\n")
        rc, res, text = finish(p)
        pidf = res.get("purchase_id")
        check("F5: the question and the summary Touch ID's dialog and a passphrase approval sign both name the payment form's site",
              PAYF in term and any(PAYF in q.get("summary", "") for q in rq), (term[-600:], rq))
        longf = ["https://" + c * 40 + "." + c * 30 + ".example" for c in "abc"]      # 87 characters each: valid, and long
        n5, t5b = len(calls()), len(u.text())
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", *sum((["--frame-origin", f] for f in longf), []))
        check("F5: payment-form sites the approval can't show in full (three long ones): refused before anything is asked, never cut short",
              r.returncode == 2 and "can't show every site" in js(r.stdout).get("reason", "") and "Type visa" not in u.text()[t5b:], r.stdout[-300:])
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--frame-origin", "https://" + ("d" * 50 + ".") * 2 + "example")
        check("F5: a site address longer than an approval shows in full: refused", r.returncode == 2 and "longer than an approval can show" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", *sum((["--frame-origin", f"https://pf{i}.example"] for i in range(4)), []))
        check("F5: at most three payment-form sites", r.returncode != 0 and "Type visa" not in u.text()[t5b:], (r.returncode, r.stdout[-200:]))
        midf = ["https://" + c * 30 + "." + c * 15 + ".example" for c in "ef"]          # 62 characters each
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--frame-origin", midf[0], "--frame-origin", midf[1])
        u.expect("Type visa to allow this one purchase", timeout=60)
        rq5 = json.loads(client("requests", "--json").stdout or "{}").get("requests", [])
        u.send("no\n")
        finish(p)
        check("F5: two payment-form sites: the summary Touch ID shows and a passphrase approval signs has every site in full, "
              "within what the dialog shows, and what approving means", any(all(f in q.get("summary", "") for f in midf) and SITE in q.get("summary", "")
                                                                           and "can be charged again" in q.get("summary", "") for q in rq5)
              and not any(c["has_value"] for c in calls()[n5:]), rq5)
        sflag("move-on-fill", PAYF)
        n6 = len(calls())
        r = client("pay", "--purchase", pidf or "0" * 32, "--vm", "pvm", "--field", "number")
        sunflag("move-on-fill")
        fills6 = [c for c in calls()[n6:] if c["mode"] == "fill"]
        check("F4: a tab that moves to the payment form's own site between the check and the fill: the VM's helper refuses it as the tab "
              "(top_origins: the shop only), so the number isn't typed there", r.returncode != 0 and fills6 and all(c["top_origins"] == [SITE] for c in fills6)
              and json.loads(open(stub_log).read().splitlines()[-1]).get("top_origins") == [SITE], (r.stdout[-300:], fills6[:1]))
        sflag("no-ack")
        n7 = len(calls())
        r = client("pay", "--purchase", pidf or "0" * 32, "--vm", "pvm", "--field", "cvc")
        sunflag("no-ack")
        check("F4: a VM whose fill helper doesn't say it held the tab to the shop (an older image): refused before anything is typed",
              r.returncode == 2 and "older than purchases need" in js(r.stdout).get("reason", "") and not any(c["has_value"] for c in calls()[n7:]), r.stdout[-300:])
        client("pay", "--purchase", pidf or "0" * 32, "--done")
        sflag("read-text", "Total $3.00")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--frame-origin", PAYF)
        check("F5: an agent card refuses a payment form's site nobody allowed (no question would name it)", r.returncode == 2
              and "--allow-frame" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        rc, out = tty([BIN, "policy", "agentcard", "--allow-frame", PAYF], [("Vault passphrase", PASS + "\n")])
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--frame-origin", PAYF)
        check("F5: ...and allows it once the user named it for that card (policy --allow-frame, their terminal)", rc == 0 and r.returncode == 0
              and js(r.stdout).get("frame_origins") == [PAYF], (out[-200:], r.stdout[-300:]))
        client("pay", "--purchase", js(r.stdout).get("purchase_id") or "0" * 32, "--done")

    # --- 6a. F14: a purchase is bound to whoever started it; outside an agent session it can't be resumed ----------------
    with section('6a. F14: a purchase is bound to whoever started it'):
        # A card of its own, so what's spent elsewhere in this run doesn't decide these checks.
        rc0, out0 = card_tty("appcard", ["--label", "App Card", "--pay", "agent", "--currency", "USD", "--max-purchase", "50"], number=NUMBER2)
        sflag("read-text", "Total $1.00")
        t14 = len(u.text())
        rc, out = orphan([BIN, "pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total"], env)
        outputs.append(out)
        check("F14: outside any agent session (a process launchd adopted) a purchase that would need resuming is refused, nothing asked",
              rc == 2 and "doesn't come from an agent session" in js(out).get("reason", "") and "Type visa to allow" not in u.text()[t14:], out[-300:])
        n14 = len(calls())
        rc, out = orphan([BIN, "pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc"], env)
        k14 = js(out).get("purchase_id") or "0" * 32
        rc2, out2 = orphan([BIN, "pay", "--purchase", k14, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc"], env)
        r = client("pay", "--purchase", k14, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        rc3, out3 = orphan([BIN, "pay", "--purchase", k14, "--done"], env)
        outputs.extend([out, out2, out3])
        check("F14: ...one that names its parts fills them in its own call; its key resumes it for nobody (another process outside a "
              "session, or an agent session), but still ends it", rc == 0 and js(out).get("filled") == ["number"] and rc2 == 2
              and "can't be resumed" in js(out2).get("reason", "") and r.returncode == 2 and "can't be resumed" in js(r.stdout).get("reason", "")
              and sum(1 for c in calls()[n14:] if c["has_value"]) == 1 and rc3 == 0, (rc0, out[-300:], out2[-300:], r.stdout[-200:], out3[-200:]))
        # Codex's app server runs every conversation in one process: a purchase it starts is bound to that process.
        fakecodex = os.path.join(td, "fakecodex", "codex")
        os.makedirs(os.path.dirname(fakecodex))
        os.makedirs(os.path.join(td, "app-server"))
        rcc = subprocess.run(["cc", "-o", fakecodex, fake + ".c"], capture_output=True, text=True)
        o1, o2, o3, ready, go = (os.path.join(td, "as-" + x) for x in ("start", "fill", "done", "ready", "go"))
        script = ('"$0" pay appcard --vm pvm --origin "$1" --total-selector "#total" > "$2"; '
                  'k=$(sed -n \'s/.*"purchase_id" *: *"\\([0-9a-f]\\{32\\}\\)".*/\\1/p\' "$2"); '
                  '"$0" pay --purchase "$k" --vm pvm --field number --selector "#cc" > "$3"; : > "$5"; '
                  'n=0; while [ ! -f "$6" ] && [ $n -lt 900 ]; do sleep 0.1; n=$((n+1)); done; '
                  '"$0" pay --purchase "$k" --done > "$4"')
        ap = subprocess.Popen([fakecodex, "app-server", "/bin/sh", "-c", script, BIN, SITE, o1, o2, o3, ready, go], cwd=td, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bgs.append(ap)
        deadline = time.time() + 90
        while not os.path.exists(ready) and ap.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        rd = lambda f: open(f).read() if os.path.exists(f) else ""
        k_as = js(rd(o1)).get("purchase_id") or "0" * 32
        r = client("pay", "--purchase", k_as, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        open(go, "w").close()
        ap.wait(60)
        outputs.extend([rd(o1), rd(o2), rd(o3)])
        check("F14: a purchase Codex's app server started is bound to that app server: it fills a part and ends it, while an agent session "
              "with its key is refused", rcc.returncode == 0 and js(rd(o1)).get("ok") is True and js(rd(o2)).get("filled") == ["number"]
              and r.returncode == 2 and "another Codex app server" in js(r.stdout).get("reason", "") and js(rd(o3)).get("outcome") == "done",
              (rd(o1)[-300:], rd(o2)[-300:], r.stdout[-300:], rd(o3)[-200:]))

    # --- 6c. R15: no card part leaves until the hub for that VM has said it masks for this purchase -----------------------
    with section('6c. R15: no card part before the hub says it masks'):
        acker_on.clear()
        ackf = cardmask_lib.ack_path(vdir, "pvm") if hasattr(cardmask_lib, "ack_path") else os.path.join(vdir, "purchases-ack-pvm.json")
        if os.path.exists(ackf):
            os.remove(ackf)
        sflag("read-text", "Total $2.00")
        n15b = len(calls())
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc")
        acker_on.set()
        check("R15: with no hub saying it has seen the purchase (so masks card details for that VM), the first part isn't sent: "
              "refused after a short wait, nothing typed", r.returncode != 0 and "hasn't confirmed it hides card details" in js(r.stdout).get("reason", "")
              and not any(c["has_value"] for c in calls()[n15b:]), r.stdout[-300:])
        client("pay", "--purchase", js(r.stdout).get("purchase_id") or "0" * 32, "--done")

    # --- 6d. R21: the hub's word is checked against the hub (its process, when that started, its lock, a private folder) ---
    with section('6d. R21: the hub ack is checked against the hub'):
        acker_on.clear()
        time.sleep(0.2)
        ackf = cardmask_lib.ack_path(vdir, "pvm") if hasattr(cardmask_lib, "ack_path") else os.path.join(vdir, "purchases-ack-pvm.json")
        ackd = os.path.dirname(ackf)
        os.makedirs(ackd, mode=0o700, exist_ok=True)
        sleeper = subprocess.Popen(["sleep", "120"])
        procs.append(sleeper)
        loose_lock = os.path.join(hub_lock_dir, "loose.pid")      # a lock file nobody holds, naming this process
        with open(loose_lock, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "start": my_start}))
        os.chmod(loose_lock, 0o600)
        ps_sleep = cardmask_lib.process_start(sleeper.pid)[0] if hasattr(cardmask_lib, "process_start") else None
        forms = {
            "another process": {"pid": sleeper.pid, "start": ps_sleep, "lock": hub_lock},
            "a pid used again": {"pid": os.getpid(), "start": [1, 0], "lock": hub_lock},
            "a lock nobody holds": {"pid": os.getpid(), "start": my_start, "lock": loose_lock},
            "a folder others can read": {"pid": os.getpid(), "start": my_start, "lock": hub_lock},
        }
        results21 = {}
        for form, rec in forms.items():
            forging = threading.Event()

            def forge(rec=rec, forging=forging):
                while not forging.is_set():
                    seen_ = sorted(str(h["ack"]) for h in cardmask_lib.active_purchases(vdir, "pvm") if isinstance(h.get("ack"), str))
                    try:
                        tmp = ackf + ".forge"
                        with open(tmp, "w") as f:
                            json.dump(dict(rec, v=2, vm="pvm", seen=seen_, t=time.time()), f)
                        os.chmod(tmp, 0o600)
                        os.replace(tmp, ackf)
                    except OSError:
                        pass
                    forging.wait(0.05)
            if form == "a folder others can read":
                os.chmod(ackd, 0o750)
            ft = threading.Thread(target=forge, daemon=True)
            ft.start()
            sflag("read-text", "Total $2.00")
            n21 = len(calls())
            r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc")
            forging.set()
            ft.join(2)
            os.chmod(ackd, 0o700)
            results21[form] = (r.returncode, js(r.stdout).get("reason", "")[:220], any(c["has_value"] for c in calls()[n21:]))
            client("pay", "--purchase", js(r.stdout).get("purchase_id") or "0" * 32, "--done")
        sleeper.kill()
        sleeper.wait()
        try:
            os.remove(ackf)
        except OSError:
            pass
        acker_on.set()
        check("R21: the hub's word is checked against the hub: an ack naming another live process of this user's, a process "
              "that started at another time (a pid used again), a lock nobody holds, or kept in a folder others can read is "
              "refused, nothing typed, each for its own reason", all(rc != 0 and "hasn't confirmed it hides card details" in why and not typed
                                                                    for rc, why, typed in results21.values())
              and all(w in results21.get(f, (0, "", 0))[1] for f, w in (("another process", "the hub's lock names another process"),
                                                                       ("a pid used again", "the process it names isn't running"),
                                                                       ("a lock nobody holds", "nothing holds its lock"),
                                                                       ("a folder others can read", "ack folder isn't a private folder"))),
              results21)

    # --- 6e. R27: the masks last through the whole fill and a settle margin, whenever the purchase ends -----------------
    with section('6e. R27: masks through the whole fill and a margin'):
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        slow27 = os.path.join(td, "slow-release")      # a stand-in for a slow provider check, right before a part is sent
        slowhook37 = os.path.join(td, "slow-hook")     # a stand-in for a slow wait hook inside the write to ssh
        u = start_unlock({"MCOMP_VAULT_TEST_PURCHASE_SECONDS": "8",       # purchases last 8 s in this unlock
                          "MCOMP_VAULT_TEST_SLOW_RELEASE": slow27, "MCOMP_VAULT_TEST_SLOW_HOOK": slowhook37})
        run_state["u"] = u

        def masked27(text):
            return cardmask_lib.mask_text(text, cardmask_lib.active_purchases(vdir, "pvm"))
        # A fill that outlasts its purchase: masked while it runs, and after the purchase has ended.
        sflag("read-text", "Total $2.00")
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k27 = js(r.stdout).get("purchase_id") or "0" * 32
        sflag("hold-fill")
        p = bg("pay", "--purchase", k27, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        for _ in range(200):
            if os.path.exists(os.path.join(stubdir, "holding-fill")):
                break
            time.sleep(0.1)
        held27 = os.path.exists(os.path.join(stubdir, "holding-fill"))
        time.sleep(9)                                    # past the purchase's 8 s; the fill still runs
        during27 = masked27("CVC: 123")
        sflag("release-fill")
        rc27, res27, _ = finish(p)
        time.sleep(1.5)                                  # the vault's sweep has ended the purchase
        after27 = masked27("CVC: 123")
        client("pay", "--purchase", k27, "--done")      # (ended already: its time is up; a build without that ends it here)
        # A purchase its requester ends right after a fill: still masked for the margin.
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k27b = js(r.stdout).get("purchase_id") or "0" * 32
        r2 = client("pay", "--purchase", k27b, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        client("pay", "--purchase", k27b, "--done")
        done27 = masked27("CVC: 123")
        check("R27: the hub's masks last through a whole fill and a settle margin after it: a fill still running past its purchase's "
              "time is masked while it runs and after the purchase ends, and a purchase ended right after a fill stays masked",
              held27 and rc27 == 0 and res27.get("ok") is True and r2.returncode == 0
              and all("123" not in x for x in (during27, after27, done27)), (held27, rc27, res27, during27, after27, done27))
        # A purchase whose time ends while its page is checked: its time is checked again right before the part leaves.
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k27c = js(r.stdout).get("purchase_id") or "0" * 32
        sflag("hold-check")
        n27 = len(calls())
        p = bg("pay", "--purchase", k27c, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        for _ in range(200):
            if os.path.exists(os.path.join(stubdir, "holding")):
                break
            time.sleep(0.1)
        time.sleep(9)
        sflag("release")
        rc27c, res27c, _ = finish(p)
        check("R27: a purchase whose time ends while its page is being checked sends nothing (checked again right before release)",
              rc27c != 0 and "expired" in res27c.get("reason", "") and not any(c["has_value"] for c in calls()[n27:]), (rc27c, res27c))
        # R32: its time runs out during the last slow step before the part goes (a provider's check of a computer elsewhere,
        # stood in for here by a wait right before release): nothing is sent.
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k32 = js(r.stdout).get("purchase_id") or "0" * 32
        open(slow27, "w").write("10")
        n32 = len(calls())
        try:
            r32 = client("pay", "--purchase", k32, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        finally:
            os.remove(slow27)
        check("R32: a purchase whose time runs out during the last slow step before its part goes (a provider's check, stood in "
              "for) sends nothing: its time is checked again right before the bytes are written to ssh",
              r32.returncode != 0 and "expired before its part was sent" in js(r32.stdout).get("reason", "")
              and not any(c["has_value"] for c in calls()[n32:]), r32.stdout[-300:])
        # R37: and when the time runs out after that, inside the step that writes to ssh (its wait hook's work, stood in
        # for): checked again right before the bytes are written; nothing is, and ssh is ended.
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k37 = js(r.stdout).get("purchase_id") or "0" * 32
        open(slowhook37, "w").write("10")
        n37 = len(calls())
        try:
            r37 = client("pay", "--purchase", k37, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        finally:
            os.remove(slowhook37)
        check("R37: a purchase whose time runs out inside the write to ssh (after its wait hook's work, stood in for) sends "
              "nothing: the last check is right before the bytes are written", r37.returncode != 0
              and "expired before its part was sent" in js(r37.stdout).get("reason", "") and not any(c["has_value"] for c in calls()[n37:]),
              r37.stdout[-300:])
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        u = start_unlock()
        run_state["u"] = u

    # --- 6f. R30: --done ends a purchase and takes nothing else ---------------------------------------------------------
    with section('6f. R30: pay --done takes nothing else'):
        sflag("read-text", "Total $2.00")
        n30 = len(calls())
        ra = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc", "--done")
        rb = client("pay", "--done")
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k30 = js(r.stdout).get("purchase_id") or "0" * 32
        rf = client("pay", "--purchase", k30, "--vm", "pvm", "--field", "number", "--selector", "#cc", "--done")
        ro = client("pay", "--purchase", k30, "--done", "--vm", "othervm")
        rk = client("pay", "--purchase", k30, "--done", "--vm", "pvm")
        check("R30: the vault reads --done only as `pay --purchase KEY --done [--vm its VM]`: a new purchase with --done added, "
              "--done alone and --done with a fill are refused (usage), another VM is refused, nothing is typed; its own VM ends it",
              ra.returncode == 64 and rb.returncode == 64 and rf.returncode == 64 and ro.returncode == 2
              and "is on VM pvm" in js(ro.stdout).get("reason", "") and rk.returncode == 0 and js(rk.stdout).get("outcome") == "done"
              and not any(c["has_value"] for c in calls()[n30:]),
              (ra.returncode, rb.returncode, rf.returncode, ro.returncode, ro.stdout[-200:], rk.returncode, rk.stdout[-200:]))

    # --- 6g. R33: a new unlock keeps an earlier one's masks while their window lasts -----------------------------------
    with section('6g. R33: a new unlock keeps the earlier masks'):
        active_f = os.path.join(vdir, "purchases-active.json")

        def entries33():
            try:
                return json.load(open(active_f)).get("purchases", [])
            except (OSError, ValueError):
                return []

        def mk33(e):
            return json.dumps({k: v for k, v in e.items() if k not in ("until", "ack", "filled")}, sort_keys=True)
        sflag("read-text", "Total $2.00")
        before33 = {mk33(e) for e in entries33()}
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k33 = js(r.stdout).get("purchase_id") or "0" * 32
        client("pay", "--purchase", k33, "--vm", "pvm", "--field", "cvc", "--selector", "#cvc")
        a33 = {mk33(e) for e in entries33()} - before33                    # purchase A's own entry
        u.send("\x03")                                  # lock: A's masks stay for their window
        u.wait(20)
        outputs.append(u.text())
        u = start_unlock()                               # a new unlock that buys nothing, then locks again
        run_state["u"] = u
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        after_relock = {mk33(e) for e in entries33()}
        u = start_unlock()
        run_state["u"] = u
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")      # purchase B
        kb33 = js(r.stdout).get("purchase_id") or "0" * 32
        after_b = {mk33(e) for e in entries33()}
        client("pay", "--purchase", kb33, "--done")
        # R38: however many there are: 300 entries still in their window survive the next unlock and its first write.
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        filler = [{"vm": "fillervm", "until": int(time.time()) + 45, "cvc_len": 3, "ack": f"f{i}", "n": i} for i in range(300)]
        cur = entries33()
        with open(active_f, "w") as f:
            json.dump({"v": 1, "purchases": cur + filler}, f)
        os.chmod(active_f, 0o600)
        u = start_unlock()
        run_state["u"] = u
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        kc38 = js(r.stdout).get("purchase_id") or "0" * 32
        kept38 = {e.get("n") for e in entries33() if e.get("vm") == "fillervm"}
        client("pay", "--purchase", kc38, "--done")
        check("R33: a new unlock keeps an earlier unlock's masks in the hub's file while their window lasts: after a fill, a "
              "lock, an unlock that buys nothing and locks again, then a new purchase, the first purchase's masks are still there",
              len(a33) == 1 and a33 <= after_relock and a33 <= after_b and len(after_b - a33) >= 1, (len(a33), len(after_relock), len(after_b)))
        check("R38: an unlock keeps every earlier entry still in its window, however many (300 here): none is dropped when it "
              "next writes the file", r.returncode == 0 and kept38 == set(range(300)), (r.returncode, len(kept38)))

    # --- 6b. F15: the policy is checked with the reservation, under the vault's lock --------------------------------------
    with section('6b. F15: the policy is checked with the reservation'):
        sflag("read-text", "Total $2.00")
        r = client("pay", "appcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        k15 = js(r.stdout).get("purchase_id") or "0" * 32
        sflag("hold-check")
        n15 = len(calls())
        p = bg("pay", "--purchase", k15, "--vm", "pvm", "--field", "number", "--selector", "#cc")
        for _ in range(200):
            if os.path.exists(os.path.join(stubdir, "holding")):
                break
            time.sleep(0.1)
        held = os.path.exists(os.path.join(stubdir, "holding"))
        rc15, out15 = tty([BIN, "policy", "appcard", "--max-purchase", "1"], [("Vault passphrase", PASS + "\n")])
        sflag("release")
        rc, res, text = finish(p)
        check("F15: a policy tightened after the part's own checks began (while the page was checked): the reservation, under the "
              "vault's lock, sees it and nothing is sent", r.returncode == 0 and held and rc15 == 0 and rc == 2
              and "changed after the purchase was approved" in res.get("reason", "") and not any(c["has_value"] for c in calls()[n15:]),
              (r.stdout[-200:], held, out15[-200:], text[-300:]))
        client("pay", "--purchase", k15, "--done")

    # --- 7. totals: the payable one, shown, unambiguous ------------------------------------------------------------------------
    with section('7. totals: the payable one, shown, unambiguous'):
        t12a = len(u.text())
        sflag("read-text", "Sub-total $10.00 Shipping $2.00")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F12: \"Sub-total $10.00\" isn't the payable total (nor \"Sub total\", \"Items total\"): refused, nothing asked",
              r.returncode == 2 and "no amount follows" in js(r.stdout).get("reason", "") and "Type visa" not in u.text()[t12a:], r.stdout[-300:])
        sflag("read-text", "Total $12.00 Pay now Total $12.00")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F12: two places that show a total, even the same amount: unclear, refused (the agent hands payment over)",
              r.returncode == 2 and "unclear which is the total" in js(r.stdout).get("reason", "") and "Type visa" not in u.text()[t12a:], r.stdout[-300:])
        sflag("read-text", "Total $100.00 Total savings $1.00")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--total-label", "Total savings")
        check("F12: --total-label can't name a part of the price (\"Total savings\"): refused before anything is read or asked",
              r.returncode == 2 and "part of the price" in js(r.stdout).get("reason", "") and "Type visa" not in u.text()[t12a:], r.stdout[-300:])
        sflag("read-text", "Total $100.00 Total savings $1.00")
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        u.expect("Type visa to allow this one purchase", timeout=60)
        t12 = u.text()
        u.send("no\n")
        finish(p)
        check("F12: page mode: \"Total $100.00 ... Total savings $1.00\" is USD 100.00, never the savings", "Pay USD 100.00 with Test Visa" in t12, t12[-500:])
        sflag("read-text", "Total $12.34")
        sflag("total-visible", "0")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#hidden-total")
        sunflag("total-visible")
        check("F12: a total element that isn't shown on the page is refused (hidden text isn't a total)", r.returncode == 2
              and "isn't shown" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        sflag("read-text", "Total $12.00 Discount -$7.00 Total $5.00")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#totals")
        check("F12: two different amounts after \"Total\": unclear, refused, and the agent is told to hand payment over", r.returncode == 2
              and "different amounts" in js(r.stdout).get("reason", "") and "handoff" in js(r.stdout), r.stdout[-300:])
        sflag("read-text", "Total $0.00")
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F3, F12: a zero total is refused (a purchase of nothing can't pad the spending record)", r.returncode == 2 and "zero" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])

    # --- 8. human mode: the total after its label, then the focused field ------------------------------------------------------
    with section('8. human mode: the total after its label, then the focused f'):
        sflag("human-totals", json.dumps(["total $9.99 Shipping $1.00"]))
        sflag("human-befores", json.dumps(["Items: 2  Sub-"]))
        r = client("pay", "visa", "--vm", "pvm", "--origin", SITE, "--human")
        sunflag("human-befores")
        check("F12: human mode: a \"total\" the browser shows right after \"Sub-\" isn't the payable total: refused",
              r.returncode == 2 and "no amount follows" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        sflag("human-totals", json.dumps(["Total: $9.99 Pay now", "Total savings: $2.00"]))
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--human")
        u.expect("Type visa to allow this one purchase", timeout=60)
        check("F12: human mode: the total read after \"Total\" (not \"Total savings\") in the browser's accessibility text is in the question",
              "Pay USD 9.99 with Test Visa" in u.text())
        u.send("visa\n")
        rc, res, text = finish(p)
        pid3 = res.get("purchase_id")
        n8 = len(calls())
        r = client("pay", "--purchase", pid3 or "0" * 32, "--vm", "pvm", "--field", "number", "--human")
        hc = [c for c in calls()[n8:] if c["human"]]
        check("human mode: the number goes into the focused field as keys through the desktop account, after the total is read again "
              "and a check (field card)", rc == 0 and r.returncode == 0 and [c["mode"] for c in hc] == ["total", "check", "fill"]
              and hc[2]["value"] == NUMBER and hc[2]["field"] == "card" and NUMBER not in r.stdout + r.stderr, (text[-200:], r.stdout[-300:]))
        r = client("pay", "--purchase", pid3 or "0" * 32, "--vm", "pvm", "--field", "cvc", "--human", "--selector", "#cvc")
        check("human mode takes no selector", r.returncode != 0)
        client("pay", "--purchase", pid3 or "0" * 32, "--done")

    # --- 9. Shop's CLI: only the one the user pinned, unchanged, with a clean environment; a spend request's card ------------
    with section("9. Shop's CLI: only the one the user pinned, unchanged, with"):
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        check("F9: before the user pins Shop's CLI, shop-card refuses (the vault runs no program a request names)", r.returncode == 2
              and "shop-cli set" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--shop-cli", shop_cli)
        check("F9: a request that names a program for the vault to run is refused outright", r.returncode == 64, r.stderr[-200:])
        if not runtime:
            print("SKIP  the rest of the Shop checks: no Node 22.15 or newer here (set MCOMP_TEST_NODE)")
            raise RuntimeError("no Node for the stand-in Shop CLI")
        # N2: a link that leads out of the package isn't pinned (what it leads to could change unseen).
        badpkg = os.path.join(td, "badpkg")
        shutil.copytree(shop_pkg, badpkg)
        open(os.path.join(td, "outside.mjs"), "w").write("export default 1;\n")
        os.symlink(os.path.join(td, "outside.mjs"), os.path.join(badpkg, "bin", "helper.mjs"))
        rc, out = tty([BIN, "shop-cli", "set", "--cli", os.path.join(badpkg, "bin", "shop.mjs"), "--runtime", runtime], [("Vault passphrase", PASS + "\n")])
        check("N2: a CLI package with a link out of it isn't pinned (what the link leads to isn't in the pin)", rc != 0 and "link out of the package" in out, out[-300:])
        # N4: the runtime from the script's first line (#!/usr/bin/env node: node on this terminal's PATH), the script over 256 bytes.
        auth_path = os.path.join(home, ".config", "shop-node-wasm", "credentials.json")
        rc, out = tty([BIN, "shop-cli", "set", "--cli", shop_cli], [("Vault passphrase", PASS + "\n")],
                      extra={"PATH": os.path.dirname(runtime) + ":/usr/bin:/bin"})
        check("N4, F9: shop-cli set (the user's terminal) finds the runtime from the first line of an entry script over 256 bytes, and "
              "pins it, the libraries it loads, the package's files and the one sign-in file the CLI may use",
              rc == 0 and "Pinned." in out and runtime in out and "libraries" in out and auth_path in out and os.path.getsize(shop_cli) > 256, out[-600:])
        shopflag("not-approved")
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1",
                   extra={"NODE_OPTIONS": "--require=/tmp/evil.js", "NODE_PATH": "/tmp/evil", "PATH": "/tmp/evil:/usr/bin:/bin"})
        shopflag("not-approved", on=False)
        sc = shop_calls()
        envr = sc[-1]["env"] if sc else {}
        check("F9: the pinned CLI runs with a fixed command and a clean environment: no NODE_OPTIONS, NODE_PATH or PATH of the requester's",
              sc and "NODE_OPTIONS" not in envr and "NODE_PATH" not in envr and envr.get("PATH") == "/usr/bin:/bin"
              and sc[-1]["argv"][:3] == ["spend-request", "get", "0198e63b-1234-7abc"], envr)
        run_dir = os.path.realpath(sc[-1]["cwd"]) if sc else ""
        check("N2: it runs confined: a home, temporary and working folder of its own in the vault's folder (removed afterwards), of your "
              "home only the sign-in file (SHOP_AUTH_FILE), OpenSSL's configuration off, no add-ons, and module hooks that hold what it "
              "loads to its package (Node's --import preload)",
              sc and run_dir.startswith(os.path.realpath(vdir) + "/shop-run-") and os.path.realpath(envr.get("HOME", "-")) == run_dir
              and envr.get("TMPDIR", "").startswith(envr.get("HOME", "-")) and envr.get("SHOP_AUTH_FILE") == auth_path
              and envr.get("OPENSSL_CONF") == "/dev/null" and "--no-addons" in sc[-1]["execArgv"]
              and any(x.startswith("data:text/javascript;base64,") for x in sc[-1]["execArgv"]) and not glob.glob(os.path.join(vdir, "shop-run-*")),
              (envr, sc[-1].get("cwd") if sc else None, sc[-1].get("execArgv") if sc else None))
        check("shop-card before the user approved it in Shop: no card, and Shop's own words aren't passed on", r.returncode != 0
              and "not_approved" in js(r.stdout).get("reason", "") and not glob.glob(os.path.join(vdir, "shop-run-*")), r.stdout[-300:])
        shopflag("try-planted")
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        shopflag("try-planted", on=False)
        check("N2: a module an agent planted in a node_modules folder above the package isn't loaded: the CLI is stopped before it runs "
              "any of it", r.returncode == 2 and "outside its pinned package" in js(r.stdout).get("reason", "")
              and not os.path.exists(os.path.join(td, "planted-ran")), r.stdout[-300:])
        outside = os.path.join(td, "outside")
        os.makedirs(outside, exist_ok=True)
        open(os.path.join(outside, "escape.mjs"), "w").write("export default 'OUTSIDE CODE RAN';\n")
        open(os.path.join(outside, "planted.txt"), "w").write("planted secret\n")
        shopflag("try-escapes", content=outside)
        shopflag("not-approved")                    # no card comes of it
        n1s = len(shop_calls())
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        shopflag("try-escapes", on=False)
        shopflag("not-approved", on=False)
        esc = next((c["escapes"] for c in shop_calls()[n1s:] if "escapes" in c), {})
        check("R1: the OS sandbox stops what Node's module hooks alone didn't (the re-check's reproduction): an eval worker importing a "
              "file outside the package, a child process, and a read of a file outside it are all denied, and nothing of them ran",
              len(esc) == 3 and all(str(v).startswith("DENIED") for v in esc.values()) and not os.path.exists(os.path.join(outside, "spawn-ran")), esc)
        for flag_, what_ in (("sandbox-exec", "isn't available"), ("sandbox-break", "couldn't be applied")):
            shopflag(flag_, content="/nonexistent/sandbox-exec" if flag_ == "sandbox-exec" else "")
            n1t = len(shop_calls())
            r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
            shopflag(flag_, on=False)
            check(f"R1: with sandbox-exec {'missing' if flag_ == 'sandbox-exec' else 'failing to apply its profile'}, Shop's CLI isn't run at all",
                  r.returncode == 2 and what_ in js(r.stdout).get("reason", "") and len(shop_calls()) == n1t, r.stdout[-300:])
        # R10: its network is the run's proxy, to public hosts on 443: nothing local, no Unix socket.
        mport = srv.server_address[1]
        shopflag("proxy-allow", content=f"127.0.0.1:{mport}")
        shopflag("try-network", content=json.dumps({"local_port": mport, "unix_socket": os.path.join(vdir, "vault.sock"), "other_port": port,
                                                    "allowed": f"127.0.0.1:{mport}"}))
        shopflag("not-approved")
        n10s = len(shop_calls())
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        for f_ in ("try-network", "not-approved", "proxy-allow"):
            shopflag(f_, on=False)
        netr = next((c["network"] for c in shop_calls()[n10s:] if "network" in c), {})
        check("R10: inside the sandbox, a local service (an HTTP server on 127.0.0.1) and a Unix socket (the vault's own) can't be "
              "reached; through the run's proxy, a loopback address and a private one are refused, and only the stand-in merchant the "
              "test build allows is tunnelled to", str(netr.get("directLocal", "")).startswith("DENIED") and str(netr.get("unixSocket", "")).startswith("DENIED")
              and netr.get("proxyLocal") == "403" and netr.get("proxyPrivate") == "403" and str(netr.get("proxyAllowed", "")).startswith("200 HTTP/1."), netr)
        check("R18: the proxy wants this run's secret (from HTTPS_PROXY's userinfo): without it or with another, 407; and it serves "
              "at most 8 connections at once (a ninth gets 503)", netr.get("proxyNoAuth") == "407" and netr.get("proxyBadAuth") == "407"
              and netr.get("proxyOverCap") == "503", netr)
        if os.environ.get("MCOMP_TEST_PUBLIC_NET") == "1":
            shopflag("try-public", content="https://example.com/")
            shopflag("not-approved")
            n10p = len(shop_calls())
            client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
            shopflag("try-public", on=False)
            shopflag("not-approved", on=False)
            pub = next((c["publicNet"] for c in shop_calls()[n10p:] if "publicNet" in c), "")
            check("R10 (MCOMP_TEST_PUBLIC_NET): a real HTTPS request through the proxy, as Shop's CLI makes them, reaches a public host", pub.startswith("HTTP "), pub)
        planted_lib = os.path.join(os.path.dirname(runtime), os.path.basename(node_lib or "libnode.dylib"))
        if node_lib:
            shutil.copy2(node_lib, planted_lib)
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        if os.path.exists(planted_lib):
            os.remove(planted_lib)
        check("N2: a library planted where the runtime's loader looks first (an earlier @rpath folder) isn't run: refused",
              node_lib and r.returncode == 2 and "libraries its runtime loads changed" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        shopflag("no-business-url")
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        shopflag("no-business-url", on=False)
        check("F13: an answer without the store's site isn't imported as an unrestricted card", r.returncode != 0
              and "no store site" in js(r.stdout).get("reason", "") and not glob.glob(os.path.join(vdir, "shop-run-*")), r.stdout[-300:])
        shopflag("wrong-id")
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        shopflag("wrong-id", on=False)
        check("F13: a card file for another spend request isn't imported", r.returncode != 0 and "isn't for this spend request" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        r = client("shop-card", "shopcard", "--spend-request", "0198e63b-1234-7abc", "--wait", "1")
        j = js(r.stdout)
        check("shop-card: the unlock process runs the pinned CLI with --output-file into the CLI's own folder, stores the single-use card "
              "(its store's site, its approved limit as the cap) and removes the file; nothing of the card in the output",
              r.returncode == 0 and j.get("single_use") is True and j.get("origin") == SITE and j.get("caps") == "at most USD 25.00 a purchase"
              and not glob.glob(os.path.join(vdir, "shop-run-*")) and SHOP_NUMBER not in r.stdout + r.stderr, r.stdout[-400:])
        rc, out = tty([BIN, "policy", "shopcard", "--pay", "agent"], [("Vault passphrase", PASS + "\n")])
        check("a single-use Shop card can't become an agent card", rc != 0 and "always asks" in out, out[-200:])
        sflag("read-text", "Total $30.00")
        r = client("pay", "shopcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("...over its approved limit: refused", r.returncode == 2 and "USD 25.00 a purchase" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        sflag("read-text", "Total $15.79")
        p = bg("pay", "shopcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total", "--fill", "number=#cc", "--fill", "cvc=#cvc")
        u.expect("Type shopcard to allow this one purchase", timeout=60)
        tsc = u.text()
        u.send("shopcard\n")
        rc, res, text = finish(p)
        r = client("pay", "shopcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("its one purchase fills it (still asking you; no reusable-card line for a single-use card); a second purchase is refused",
              rc == 0 and res.get("filled") == ["number", "cvc"] and r.returncode == 2 and "single-use" in js(r.stdout).get("reason", "")
              and "a reusable card can be charged again" not in tsc[-900:], (text[-300:], r.stdout[-200:]))
        client("pay", "--purchase", res.get("purchase_id") or "0" * 32, "--done")
        r = client("list", "--json")
        check("...and when that purchase ends the single-use card is deleted from the vault", r.returncode == 0
              and "shopcard" not in [s["name"] for s in json.loads(r.stdout).get("secrets", [])]
              and any(x.get("op") == "pay_end" and x.get("single_use_deleted") is True for x in audit_recs()))

    # --- 10. Shop checkouts: through the pinned CLI, bound to the checkout that was approved --------------------------------
    with section('10. Shop checkouts: through the pinned CLI, bound to the che'):
        self_sh = os.path.join(td, "mc-self")
        open(self_sh, "w").write(f"#!/bin/sh\n[ \"$1\" = secrets ] && shift\nexec {BIN} \"$@\"\n")
        os.chmod(self_sh, 0o755)
        inst = os.path.join(td, "instrument.json")
        open(inst, "w").write(json.dumps({"id": "instrument-a", "handler_id": "shop_pay", "type": "shop_pay"}))
        senv = dict(env, MCOMP_SHOP_CLI=shop_cli, MCOMP_SELF=self_sh, MCOMP_VM_NAME="pvm", PATH=os.path.dirname(runtime or "/x") + ":" + env["PATH"])
        shopflag("gets", content="0")
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "shop.py"), "complete", "gid://shopify/Checkout/abc123?key=k1",
                            "--shop", "stand-in-store.example", "--instrument", "@" + inst, "--account", "myshop"],
                           capture_output=True, text=True, env=senv, timeout=180)
        outputs.append(r.stdout + r.stderr)
        j = js(r.stdout)
        sc = shop_calls()
        check("metacomputer shop complete: the vault reads the checkout, approves it under the shop entry's policy, reads it again, and "
              "completes it once with the instrument on stdin; the account's details never printed", r.returncode == 0
              and j.get("outcome") == "completed" and j.get("order") == "#1042" and j.get("total") == "USD 15.79"
              and [c["argv"][:2] for c in sc[-3:]] == [["checkout", "get"], ["checkout", "get"], ["checkout", "complete"]]
              and "instrument-a" in sc[-1]["stdin"] and "never-print" not in r.stdout + r.stderr
              and any(x.get("route") == "shop" and x.get("outcome") == "completed" for x in audit_recs()), (r.stdout[-400:], r.stderr[-300:]))
        shopflag("reprice-after-first")
        shopflag("gets", content="0")
        n10 = len(shop_calls())
        r = client("shop-complete", "gid://shopify/Checkout/abc124", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst,
                   "--account", "myshop")
        shopflag("reprice-after-first", on=False)
        check("F11: a checkout repriced while it was being approved is refused at the second read; complete is never run",
              r.returncode == 2 and "changed" in js(r.stdout).get("reason", "") and not any(c["argv"][:2] == ["checkout", "complete"] for c in shop_calls()[n10:]),
              r.stdout[-300:])
        open(os.path.join(td, "other-inst.json"), "w").write(json.dumps({"id": "instrument-z", "handler_id": "shop_pay", "type": "shop_pay"}))
        r = client("shop-complete", "gid://shopify/Checkout/abc125", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument",
                   os.path.join(td, "other-inst.json"))
        check("F11: an instrument the checkout doesn't offer is refused", r.returncode == 2 and "isn't one this checkout offers" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        shopflag("checkout-total", content="2100")
        r = client("shop-complete", "gid://shopify/Checkout/abc126", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst,
                   "--account", "myshop")
        shopflag("checkout-total", on=False)
        check("...over the shop entry's cap (21.00 > 20.00): refused", r.returncode == 2 and "a purchase" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        shopflag("checkout-status", content="requires_escalation")
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "shop.py"), "complete", "gid://shopify/Checkout/abc127",
                            "--shop", "stand-in-store.example", "--instrument", "@" + inst], capture_output=True, text=True, env=senv, timeout=120)
        shopflag("checkout-status", on=False)
        check("a checkout that needs the buyer: nothing is approved or completed, the link and the handoff are given", r.returncode == 2
              and js(r.stdout).get("continue_url", "").startswith("https://") and "handoff --why payment" in js(r.stdout).get("handoff", ""), r.stdout[-300:])
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "shop.py"), "status", "--json"], capture_output=True, text=True, env=senv, timeout=60)
        check("metacomputer shop status: installed and signed in, without the account's details", js(r.stdout).get("signed_in") is True
              and "never-print" not in r.stdout, r.stdout[-200:])
        # N5: a fraction in the checkout that changed between the reads (a line item's quantity 1.1, then 1.9).
        shopflag("requantity-after-first")
        shopflag("gets", content="0")
        n5s = len(shop_calls())
        r = client("shop-complete", "gid://shopify/Checkout/n5", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "myshop")
        shopflag("requantity-after-first", on=False)
        check("N5: a checkout whose line item's quantity changed from 1.1 to 1.9 between the reads (numbers compared exactly, the whole "
              "checkout): refused, complete never run", r.returncode == 2 and "changed" in js(r.stdout).get("reason", "")
              and not any(c["argv"][:2] == ["checkout", "complete"] for c in shop_calls()[n5s:]), r.stdout[-300:])
        shopflag("requantity-big")
        shopflag("gets", content="0")
        n5b = len(shop_calls())
        r = client("shop-complete", "gid://shopify/Checkout/n5b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "myshop")
        shopflag("requantity-big", on=False)
        check("N5: two integers past 64 bits (18446744073709551616, then ...617) compared by their text, never rounded through a "
              "floating-point number: refused, complete never run", r.returncode == 2 and "changed" in js(r.stdout).get("reason", "")
              and not any(c.get("argv", [])[:2] == ["checkout", "complete"] for c in shop_calls()[n5b:]), r.stdout[-300:])
        shopflag("instrument-big")
        shopflag("gets", content="0")
        bigi = os.path.join(td, "instrument-big.json")
        open(bigi, "w").write('{"id":"instrument-a","handler_id":"shop_pay","type":"shop_pay","n":18446744073709551617}')
        r = client("shop-complete", "gid://shopify/Checkout/n5i", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", bigi, "--account", "myshop")
        shopflag("instrument-big", on=False)
        check("N5: the instrument is matched by its exact text too: one whose number differs past 64 bits from the one offered is refused",
              r.returncode == 2 and "isn't one this checkout offers" in js(r.stdout).get("reason", ""), r.stdout[-300:])
        # N6, F15: a policy tightened while the approval waited.
        rc, out = tty([BIN, "set", "askshop", "--kind", "shop", "--label", "Ask Shop", "--currency", "USD", "--max-purchase", "50"], [("Vault passphrase", PASS + "\n")])
        shopflag("gets", content="0")
        n6s = len(shop_calls())
        p = bg("shop-complete", "gid://shopify/Checkout/n6", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "askshop")
        u.expect("Type askshop to allow this one purchase", timeout=60)
        rc6, out6 = tty([BIN, "policy", "askshop", "--max-purchase", "1"], [("Vault passphrase", PASS + "\n")])
        u.send("askshop\n")
        rc, res, text = finish(p)
        check("N6, F15: a shop entry's policy tightened while its approval waited (a USD 1 cap): the approval completes nothing",
              rc6 == 0 and rc == 2 and "changed after the purchase was approved" in res.get("reason", "")
              and not any(c["argv"][:2] == ["checkout", "complete"] for c in shop_calls()[n6s:]), (out6[-200:], text[-300:]))
        # N7: what Shop says it charged, checked after the completion.
        rc, out = tty([BIN, "set", "n7shop", "--kind", "shop", "--label", "N7 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        shopflag("complete-total", content="1700")
        shopflag("gets", content="0")
        r = client("shop-complete", "gid://shopify/Checkout/n7a", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "n7shop")
        shopflag("complete-total", on=False)
        j = js(r.stdout)
        check("N7: Shop reports a higher charge than approved (17.00, not 15.79): said plainly (outcome, charged, unverified, a warning), "
              "and the extra goes in the spending record (17.00 in the period)", r.returncode == 0 and j.get("outcome") == "completed_with_a_higher_total"
              and j.get("charged") == "USD 17.00" and j.get("verified") is False and "check the order in Shop" in j.get("warning", "")
              and spent("n7shop") == "USD 17.00", (r.stdout[-400:], spent("n7shop")))
        shopflag("complete-no-total")
        shopflag("gets", content="0")
        r = client("shop-complete", "gid://shopify/Checkout/n7b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "n7shop")
        shopflag("complete-no-total", on=False)
        check("N7: Shop's answer without what it charged: completed but unverified, with a warning", r.returncode == 0
              and js(r.stdout).get("outcome") == "completed_unverified" and js(r.stdout).get("verified") is False, r.stdout[-300:])
        tty([BIN, "spend-resolve", "n7shop"], [("Vault passphrase", PASS + "\n")])      # (R17: it stays unsettled until then)
        shopflag("complete-currency", content="EUR")
        shopflag("gets", content="0")
        r = client("shop-complete", "gid://shopify/Checkout/n7c", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "n7shop")
        shopflag("complete-currency", on=False)
        shopflag("gets", content="0")
        r2 = client("shop-complete", "gid://shopify/Checkout/n7d", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "n7shop")
        check("N7: a charge in another currency than approved (EUR, not USD): unverified, said; it goes in the spending record, and the "
              "entry's caps (which can't count it) refuse further purchases until it ages out", r.returncode == 0
              and js(r.stdout).get("outcome") == "completed_in_another_currency" and js(r.stdout).get("charged") == "EUR 15.79"
              and r2.returncode == 2 and "spending in EUR" in js(r2.stdout).get("reason", ""), (r.stdout[-300:], r2.stdout[-300:]))
        # R6: two currencies for one charge never verify, whichever comes first.
        rc, out = tty([BIN, "set", "r6shop", "--kind", "shop", "--label", "R6 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        shopflag("complete-total", content="1000")
        shopflag("complete-total-currency", content="EUR")
        shopflag("gets", content="0")
        r = client("shop-complete", "gid://shopify/Checkout/r6", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r6shop")
        shopflag("complete-total", on=False)
        shopflag("complete-total-currency", on=False)
        check("R6: Shop's answer names USD for the charge and EUR on its total: reported as unverified, never verified",
              r.returncode == 0 and js(r.stdout).get("outcome") == "completed_unverified" and js(r.stdout).get("verified") is False
              and "conflicting currencies" in js(r.stdout).get("warning", ""), r.stdout[-400:])
        # R7: extra spending the vault can't record blocks the entry until it's recorded; never undercounted.
        rc, out = tty([BIN, "set", "r7shop", "--kind", "shop", "--label", "R7 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        shopflag("complete-total", content="1700")
        shopflag("hold-complete")
        shopflag("gets", content="0")
        p = bg("shop-complete", "gid://shopify/Checkout/r7a", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r7shop")
        for _ in range(300):
            if os.path.exists(os.path.join(shopdata, "holding-complete")):
                break
            time.sleep(0.1)
        os.chmod(vdir, 0o500)
        try:
            shopflag("release-complete")
            rc, res, text = finish(p)
            shopflag("complete-total", on=False)
            shopflag("gets", content="0")
            r2 = client("shop-complete", "gid://shopify/Checkout/r7b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r7shop")
        finally:
            os.chmod(vdir, 0o700)
        shopflag("gets", content="0")
        r3 = client("shop-complete", "gid://shopify/Checkout/r7c", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r7shop")
        check("R7: Shop charged more (17.00 > 15.79) while the vault couldn't be written: said (spending_recorded false), the entry's "
              "next purchase refused until it's recorded, then recorded and counted once the vault can be written (USD 32.79 in the period)",
              rc == 0 and res.get("spending_recorded") is False and "couldn't be recorded" in res.get("warning", "")
              and r2.returncode == 2 and ("couldn't be recorded in the vault yet" in js(r2.stdout).get("reason", "") or "isn't settled yet" in js(r2.stdout).get("reason", ""))
              and r3.returncode == 0 and spent("r7shop") == "USD 32.79", (text[-400:], r2.stdout[-300:], r3.stdout[-300:], spent("r7shop")))
        # R8: a record in spend-pending.jsonl the vault can't verify never lowers spending, and blocks until the user checks.
        rc, out = tty([BIN, "set", "r8shop", "--kind", "shop", "--label", "R8 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        vj = os.path.join(vdir, "vault.json")
        pend = os.path.join(vdir, "spend-pending.jsonl")
        shopflag("complete-total", content="1700")
        shopflag("hold-complete")
        shopflag("gets", content="0")
        p = bg("shop-complete", "gid://shopify/Checkout/r8a", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r8shop")
        for _ in range(300):
            if os.path.exists(os.path.join(shopdata, "holding-complete")):
                break
            time.sleep(0.1)
        subprocess.run(["chflags", "uchg", vj])                # the vault file can't be replaced: the outcome goes to the pending file
        try:
            shopflag("release-complete")
            rc8, res8, text8 = finish(p)
        finally:
            subprocess.run(["chflags", "nouchg", vj])
        shopflag("complete-total", on=False)
        plines = [l for l in (open(pend).read().splitlines() if os.path.exists(pend) else []) if l.strip()]
        card_id = next((json.loads(l).get("card") for l in plines), None)
        before8 = spent("r8shop")
        with open(pend, "a") as f:                             # an agent, as you, forges one with a negative amount
            f.write(json.dumps({"card": card_id, "origin": "https://stand-in-store.example", "amount": -100000, "currency": "USD",
                                "t": time.time(), "purchase": "forged-r8"}) + "\n")
        shopflag("gets", content="0")
        r2 = client("shop-complete", "gid://shopify/Checkout/r8b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r8shop")
        after8 = spent("r8shop")
        check("R8: a forged pending record (an observed card id, a negative amount) is never imported: purchases are refused until you "
              "check the file, spending isn't lowered, the genuine MAC'd record is recorded, and the rejection is in the audit log",
              rc8 == 0 and card_id and r2.returncode == 2 and "can't verify" in js(r2.stdout).get("reason", "") and before8 == "USD 15.79"
              and after8 == "USD 17.00" and any(x.get("event") == "payment.pending_rejected" for x in audit_recs()),
              (text8[-200:], before8, after8, r2.stdout[-300:]))
        rc, out = tty([BIN, "spend-resolve", "--discard-pending"], [("Vault passphrase", PASS + "\n")])
        shopflag("gets", content="0")
        r3 = client("shop-complete", "gid://shopify/Checkout/r8c", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r8shop")
        check("R8: ...and once you drop what can't be verified (spend-resolve --discard-pending, your terminal), purchases go on",
              rc == 0 and r3.returncode == 0 and spent("r8shop") == "USD 32.79", (out[-200:], r3.stdout[-300:], spent("r8shop")))
        # R11: an unsettled completion blocks its entry across a restart, until it's settled.
        rc, out = tty([BIN, "set", "r11shop", "--kind", "shop", "--label", "R11 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        shopflag("complete-total", content="1700")
        shopflag("hold-complete")
        shopflag("gets", content="0")
        p = bg("shop-complete", "gid://shopify/Checkout/r11a", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r11shop")
        for _ in range(300):
            if os.path.exists(os.path.join(shopdata, "holding-complete")):
                break
            time.sleep(0.1)
        os.chmod(vdir, 0o500)                                  # neither the vault nor the pending file can be written
        try:
            shopflag("release-complete")
            finish(p)
        finally:
            os.chmod(vdir, 0o700)
        shopflag("complete-total", on=False)
        u.send("\x03")                                        # the vault locks; a new unlock starts with nothing in memory
        u.wait(20)
        outputs.append(u.text())
        u = start_unlock()
        run_state["u"] = u
        shopflag("gets", content="0")
        r2 = client("shop-complete", "gid://shopify/Checkout/r11b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r11shop")
        rc, out = tty([BIN, "spend-resolve", "r11shop", "--extra", "1.21"], [("Vault passphrase", PASS + "\n")])
        shopflag("gets", content="0")
        r3 = client("shop-complete", "gid://shopify/Checkout/r11c", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r11shop")
        check("R11: a completion whose outcome couldn't be written still blocks its entry after the vault locks and unlocks again (the "
              "block was sealed in the vault with the reservation), until you settle it (spend-resolve NAME --extra, your terminal)",
              r2.returncode == 2 and "isn't settled yet" in js(r2.stdout).get("reason", "") and rc == 0 and r3.returncode == 0
              and spent("r11shop") == "USD 32.79", (r2.stdout[-300:], out[-200:], r3.stdout[-300:], spent("r11shop")))
        # R17: an outcome nobody can read settles nothing.
        rc, out = tty([BIN, "set", "r17shop", "--kind", "shop", "--label", "R17 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])
        shopflag("complete-garbage")
        shopflag("gets", content="0")
        r1 = client("shop-complete", "gid://shopify/Checkout/r17a", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r17shop")
        shopflag("complete-garbage", on=False)
        shopflag("gets", content="0")
        r2 = client("shop-complete", "gid://shopify/Checkout/r17b", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r17shop")
        check("R17: a completion whose answer can't be read (Shop may have charged anything) leaves its entry blocked: the next "
              "purchase is refused until a definite outcome or spend-resolve", r2.returncode == 2 and "isn't settled yet" in js(r2.stdout).get("reason", "")
              and "spend-resolve" in js(r1.stdout).get("warning", "") + js(r1.stdout).get("reason", ""), (r1.stdout[-300:], r2.stdout[-300:]))
        tty([BIN, "spend-resolve", "r17shop"], [("Vault passphrase", PASS + "\n")])
        # R24: only Shop's own "nothing was sent" codes settle a completion as failed; any other failure stays unknown.
        rc, out = tty([BIN, "set", "r24shop", "--kind", "shop", "--label", "R24 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "500/7d"], [("Vault passphrase", PASS + "\n")])
        r24 = {}
        for n_, env_ in (("bare", '{"ok":false}'), ("transport", '{"ok":false,"v":1,"error":{"code":"transport","retryable":true}}'),
                         ("not completed", '{"ok":false,"v":1,"error":{"code":"not_completed"}}'),
                         ("unknown code", '{"ok":false,"v":1,"error":{"code":"payment_declined_maybe"}}'),
                         ("nothing sent", '{"ok":false,"v":1,"error":{"code":"invalid_input","message":"bad instrument"}}')):
            shopflag("complete-envelope", content=env_)
            shopflag("gets", content="0")
            ra = client("shop-complete", "gid://shopify/Checkout/r24" + n_[:3], "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r24shop")
            shopflag("complete-envelope", on=False)
            shopflag("gets", content="0")
            rb = client("shop-complete", "gid://shopify/Checkout/r24b" + n_[:3], "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r24shop")
            r24[n_] = (js(ra.stdout).get("outcome"), rb.returncode, "isn't settled yet" in js(rb.stdout).get("reason", ""))
            if r24[n_][2]:
                tty([BIN, "spend-resolve", "r24shop"], [("Vault passphrase", PASS + "\n")])
        check("R24: a completion that fails with a bare {\"ok\":false}, a transport error, a checkout not completed or a code Shop's CLI "
              "doesn't use for \"nothing sent\" settles nothing (the next purchase waits for spend-resolve); only its own refusal "
              "before sending (invalid_input) settles it as failed", all(r24[k][0] == "not_confirmed" and r24[k][2] for k in
                                                                       ("bare", "transport", "not completed", "unknown code"))
              and r24["nothing sent"][0] == "failed" and r24["nothing sent"][1] == 0 and not r24["nothing sent"][2], r24)
        # R16: settlement is by the reservation's own id: a genuine record of an earlier attempt can't settle a later one.
        rc, out = tty([BIN, "set", "r16shop", "--kind", "shop", "--label", "R16 Shop", "--pay", "agent", "--currency", "USD", "--max-purchase", "100",
                       "--max-per", "200/7d"], [("Vault passphrase", PASS + "\n")])

        def held_completion(total, ck):
            shopflag("complete-total", content=total)
            shopflag("hold-complete")
            shopflag("gets", content="0")
            pp = bg("shop-complete", ck, "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r16shop")
            for _ in range(300):
                if os.path.exists(os.path.join(shopdata, "holding-complete")):
                    break
                time.sleep(0.1)
            subprocess.run(["chflags", "uchg", vj])
            try:
                shopflag("release-complete")
                out_ = finish(pp)
            finally:
                subprocess.run(["chflags", "nouchg", vj])
                shopflag("complete-total", on=False)
            return out_
        held_completion("1579", "gid://shopify/Checkout/r16")         # an attempt that charged what was approved
        saved16 = open(pend).read() if os.path.exists(pend) else ""
        held_completion("1900", "gid://shopify/Checkout/r16")         # the same checkout again, charged 3.21 more
        open(pend, "w").write(saved16)                                # the first attempt's genuine record, replayed
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        u = start_unlock()
        run_state["u"] = u
        shopflag("gets", content="0")
        r2 = client("shop-complete", "gid://shopify/Checkout/r16c", "--vm", "pvm", "--shop", "stand-in-store.example", "--instrument", inst, "--account", "r16shop")
        check("R16: a genuine record of an earlier attempt at the same checkout, replayed after a restart, doesn't settle the later one "
              "(settlement names the reservation's own id): the entry stays blocked", saved16.strip() != ""
              and r2.returncode == 2 and "isn't settled yet" in js(r2.stdout).get("reason", ""), (saved16[:120], r2.stdout[-300:]))
        tty([BIN, "spend-resolve", "r16shop", "--extra", "3.21"], [("Vault passphrase", PASS + "\n")])
        with open(shop_cli, "a") as f:
            f.write("\n// changed after it was pinned\n")
        r = client("shop-card", "shopcard2", "--spend-request", "0198e63b-1234-7abd", "--wait", "1")
        check("F9: a pinned CLI whose files changed afterwards isn't run", r.returncode == 2 and "changed since it was pinned" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])
        # N2: the runtime's own library changed after the pin.
        if node_lib:
            os.chmod(node_lib, 0o644)
            with open(node_lib, "ab") as f:
                f.write(b"\0changed")
        r = client("shop-card", "shopcard3", "--spend-request", "0198e63b-1234-7abe", "--wait", "1")
        check("N2: a library the runtime loads that changed after the pin isn't run", node_lib and r.returncode == 2
              and "libraries its runtime loads changed" in js(r.stdout).get("reason", ""), r.stdout[-300:])

    # --- 11. AP2 mandates ----------------------------------------------------------------------------------------------------
    with section('11. AP2 mandates'):
        r = client("ap2", "key")
        check("ap2 key before ap2 init: says the user makes the key", r.returncode != 0 and "ap2 init" in r.stdout + r.stderr)
        rc, out = tty([BIN, "ap2", "init"], [("Vault passphrase", PASS + "\n")])
        check("ap2 init (the user, in their own terminal): prints the public key", rc == 0 and '"kty":"EC"' in out.replace(" ", "") and "Few merchants accept AP2" in out, out[-300:])
        r = client("ap2", "key", "--json")
        jwk = js(r.stdout)
        check("ap2 key: the public JWK with its thumbprint as kid", r.returncode == 0 and jwk.get("crv") == "P-256" and jwk.get("kid"), r.stdout[-300:])
        checkout_payload = {"order_id": "o-1", "merchant": {"id": "m-1", "name": "Demo Merchant", "website": SITE},
                            "totals": [{"type": "subtotal", "amount": 1800}, {"type": "total", "amount": 1950}], "currency": "USD"}
        head = b64u(b'{"alg":"ES256","typ":"JWT"}')
        cjwt = head + "." + b64u(json.dumps(checkout_payload).encode()) + "." + b64u(b"merchant-signature-stand-in")
        cfile = os.path.join(td, "checkout.jwt")
        open(cfile, "w").write(cjwt)
        p = bg("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", cfile)
        u.expect("Type visa to allow this one purchase", timeout=60)
        term = u.text()
        u.send("visa\n")
        rc, res, text = finish(p)
        ok_ap2 = rc == 0 and res.get("ok") is True and "Sign AP2 checkout and payment mandates for USD 19.50 to Demo Merchant" in term
        cm, pm = res.get("checkout_mandate", ""), res.get("payment_mandate", "")
        detail = ""
        try:
            h1, p1, d1, si1, sig1 = sd_parts(cm)
            h2, p2, d2, si2, sig2 = sd_parts(pm)
            elem1 = d1[p1["delegate_payload"][0]["..."]][1]
            elem2 = d2[p2["delegate_payload"][0]["..."]][1]
            cj = next(v for k, v in d1.items() if k in elem1.get("_sd", []))
            chash = b64u(hashlib.sha256(cjwt.encode()).digest())
            ok_ap2 = ok_ap2 and h1 == {"alg": "ES256", "kid": jwk["kid"]} and p1.get("_sd_alg") == "sha-256" and es256_verify(jwk, si1, sig1) and es256_verify(jwk, si2, sig2)
            ok_ap2 = ok_ap2 and elem1["vct"] == "mandate.checkout.1" and elem1["checkout_hash"] == chash and cj[1:] == ["checkout_jwt", cjwt]
            ok_ap2 = ok_ap2 and elem2["vct"] == "mandate.payment.1" and elem2["transaction_id"] == chash and elem2["payment_amount"] == {"amount": 1950, "currency": "USD"}
            ok_ap2 = ok_ap2 and elem2["payee"] == {"id": "m-1", "name": "Demo Merchant", "website": SITE} and elem2["payment_instrument"]["type"] == "card"
            ok_ap2 = ok_ap2 and elem2["payment_instrument"]["description"] == "Test Visa" and elem1["exp"] - elem1["iat"] == 900
            ok_ap2 = ok_ap2 and not any(v in cm + pm for v in VALUES)
            f10_risk = elem2.get("risk_data") == {"checkout_signature_verified": False, "approved_by": "user"}
        except Exception as e:      # noqa: BLE001
            ok_ap2, f10_risk, detail = False, False, repr(e)
        check("ap2 sign: after the approval (the merchant's checkout total in the question), a Checkout Mandate (mandate.checkout.1: the "
              "checkout JWT as a disclosure, its hash) and a Payment Mandate (mandate.payment.1: payee, USD 19.50 in minor units, the card "
              "by label), each an SD-JWT signed ES256 with the vault's key (checked with openssl), no card part in either", ok_ap2, detail or text[-500:])
        check("F10: the payment mandate says the merchant's signature on the checkout wasn't verified (AP2's risk_data), and who approved it",
              f10_risk and res.get("checkout_signature_verified") is False, detail)
        sdk_py, sdk_path = os.environ.get("MCOMP_AP2_SDK_PYTHON"), os.environ.get("MCOMP_AP2_SDK_PATH")
        if sdk_py and sdk_path and cm:
            code = ("import json,sys\nsys.path.insert(0, sys.argv[1])\nfrom jwcrypto.jwk import JWK\nfrom ap2.sdk.sdjwt import sd_jwt\n"
                    "k = JWK(**{x: v for x, v in json.loads(sys.argv[2]).items() if x in ('kty','crv','x','y','kid')})\n"
                    "for t in sys.argv[3:]:\n    p = sd_jwt.verify(t, k)\n    print(json.dumps(p['delegate_payload'][0]['vct']))\n")
            r = subprocess.run([sdk_py, "-I", "-c", code, sdk_path, json.dumps(jwk), cm, pm], capture_output=True, text=True, timeout=120)
            check("AP2's own SDK verifies both mandates (ap2.sdk.sdjwt.sd_jwt.verify with the vault's public key)",
                  r.returncode == 0 and r.stdout.split() == ['"mandate.checkout.1"', '"mandate.payment.1"'], (r.stdout[-300:], r.stderr[-600:]))
        else:
            print("SKIP AP2 SDK verification (set MCOMP_AP2_SDK_PYTHON and MCOMP_AP2_SDK_PATH)")
        r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", cfile)
        check("F10: one checkout, one signature: the same checkout again is refused", r.returncode == 2 and "signed already" in js(r.stdout).get("reason", ""),
              r.stdout[-300:])

        def jwt_with(payload, header=b'{"alg":"ES256"}'):
            return b64u(header) + "." + b64u(json.dumps(payload).encode() if isinstance(payload, dict) else payload) + "." + b64u(b"sig")
        bad_cases = [("alg none", jwt_with(dict(checkout_payload, order_id="o-2"), b'{"alg":"none"}')),
                     ("no merchant", jwt_with({k: v for k, v in dict(checkout_payload, order_id="o-3").items() if k != "merchant"})),
                     ("two payable totals", jwt_with(dict(checkout_payload, order_id="o-4", totals=[{"type": "total", "amount": 1950}, {"type": "total", "amount": 50}]))),
                     ("totals that disagree", jwt_with(dict(checkout_payload, order_id="o-5", total_price=1.00))),
                     ("a field twice", jwt_with(b'{"order_id":"o-6","currency":"USD","currency":"EUR","merchant":{"id":"m-1","name":"D","website":"' + SITE.encode()
                                                + b'"},"totals":[{"type":"total","amount":100}]}')),
                     ("no checkout id", jwt_with({k: v for k, v in checkout_payload.items() if k != "order_id"}))]
        refusals = {}
        t10 = len(u.text())
        for what, tok in bad_cases:
            r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=tok)
            refusals[what] = (r.returncode, js(r.stdout).get("reason", "")[:80])
        check("F10: AP2 refuses a checkout with alg none, no merchant, two payable totals, totals that disagree, a field given twice, or "
              "no checkout id, before anything is asked", all(rc_ == 2 and "not allowed" not in why_ for rc_, why_ in refusals.values())
              and "Type visa to allow" not in u.text()[t10:], refusals)
        site_b = SITE.encode()
        nested = [("merchant.website twice", jwt_with(b'{"order_id":"o-9","currency":"USD","merchant":{"id":"m-1","name":"D","website":"' + site_b
                                                       + b'","website":"https://evil.example"},"totals":[{"type":"total","amount":100}]}')),
                  ("a total's amount twice", jwt_with(b'{"order_id":"o-10","currency":"USD","merchant":{"id":"m-1","name":"D","website":"' + site_b
                                                       + b'"},"totals":[{"type":"total","amount":100,"amount":1}]}')),
                  ("expired", jwt_with(dict(checkout_payload, order_id="o-11", exp=1000))),
                  ("addressed to someone else", jwt_with(dict(checkout_payload, order_id="o-12", aud="another-buyer-key")))]
        t10b = len(u.text())
        nref = {}
        for what, tok in nested:
            r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=tok)
            nref[what] = (r.returncode, js(r.stdout).get("reason", "")[:90])
        check("F10: AP2 also refuses a field given twice deeper in (merchant.website, a total's amount), an expired checkout (exp) and one "
              "addressed (aud) to another key than the vault's, before anything is asked",
              all(rc_ == 2 for rc_, _ in nref.values()) and "Type visa to allow" not in u.text()[t10b:]
              and "twice" in nref["merchant.website twice"][1] and "twice" in nref["a total's amount twice"][1] and "expired" in nref["expired"][1]
              and "addressed" in nref["addressed to someone else"][1], nref)
        respaced = head + "." + b64u(json.dumps(checkout_payload, indent=1).encode()) + "." + b64u(b"another-signature-stand-in")
        r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=respaced)
        check("F10: one signature per checkout by what it is (the merchant and the checkout's id), not by how it's written: the same "
              "checkout re-spaced and re-signed is refused", r.returncode == 2 and "signed already" in js(r.stdout).get("reason", "")
              and "Type visa to allow" not in u.text()[t10b:], r.stdout[-300:])
        # R5: a checkout that expires while the approval waits isn't signed.
        soon = dict(checkout_payload, order_id="o-13", exp=int(time.time()) + 6)
        sfile = os.path.join(td, "soon.jwt")
        open(sfile, "w").write(jwt_with(soon))
        p = bg("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", sfile)
        u.expect("Type visa to allow this one purchase", timeout=60)
        while time.time() < soon["exp"] + 1.5:
            time.sleep(0.2)
        u.send("visa\n")
        rc, res, text = finish(p)
        check("R5: a checkout that expired while the approval waited isn't signed: its time is checked again right before signing",
              rc == 2 and "no longer valid" in res.get("reason", "") and not res.get("payment_mandate"), text[-300:])
        t13 = len(u.text())
        mixed = dict(checkout_payload, order_id="o-14", currency="USD", totals=[{"type": "total", "amount": 1000, "currency": "EUR"}])
        r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=jwt_with(mixed))
        check("R13: a checkout whose payable total names another currency (EUR) than the checkout (USD) isn't signed, nothing asked",
              r.returncode == 2 and "two currencies" in js(r.stdout).get("reason", "") and "Type visa to allow" not in u.text()[t13:], r.stdout[-300:])
        bad = dict(checkout_payload, order_id="o-7", merchant={"id": "m-2", "name": "Other", "website": "https://other.example"})
        r = client("ap2", "sign", "visa", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=jwt_with(bad))
        check("ap2 sign refuses a checkout that names another merchant site than --origin", r.returncode == 2 and "other.example" in js(r.stdout).get("reason", ""))
        small = dict(checkout_payload, order_id="o-8", totals=[{"type": "total", "amount": 300}])
        r = client("ap2", "sign", "agentcard", "--vm", "pvm", "--origin", SITE, "--checkout-jwt", "-", stdin=jwt_with(small))
        check("F10: an agent card doesn't sign AP2 mandates for a checkout whose merchant signature isn't verified (nobody would look)",
              r.returncode == 2 and "agent card" in js(r.stdout).get("reason", "") and not js(r.stdout).get("payment_mandate"), r.stdout[-300:])

    # --- 12. Web Bot Auth: off until the user turns it on; then the vault's requests to merchants are signed -------------
    with section('12. Web Bot Auth: off until the user turns it on; then the v'):
        r = client("wba", "status")
        check("Web Bot Auth: no key, nothing signed", r.returncode == 0 and "none" in r.stdout)
        rc, out = tty([BIN, "wba", "init", "--agent", "https://agent.example"], [("Vault passphrase", PASS + "\n")])
        check("wba init (the user's terminal): a key, off until `wba on`", rc == 0 and "off until" in out, out[-300:])
        srv.got.clear()
        sflag("read-text", "Total $1.00")
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#t")
        u.expect("Type visa to allow this one purchase", timeout=60)
        u.send("no\n")
        finish(p)
        check("...while it's off the vault's requests to a merchant carry no signature", srv.got and not any("signature" in g["headers"] for g in srv.got))
        r = client("wba", "sign", "https://shop.example/x")
        check("wba sign is refused while it's off", r.returncode == 2)
        rc, out = tty([BIN, "wba", "on"], [("Vault passphrase", PASS + "\n")])
        srv.got.clear()
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#t")
        u.expect("Type visa to allow this one purchase", timeout=60)
        u.send("no\n")
        finish(p)
        d = json.loads(client("wba", "directory", "--headers").stdout or "{}")
        thumb = b64u(hashlib.sha256(json.dumps({"crv": "Ed25519", "kty": "OKP", "x": d["directory"]["keys"][0]["x"]}, separators=(",", ":"), sort_keys=True).encode()).digest())
        hdrs = [g["headers"] for g in srv.got]
        check("once on: the merchant gets Signature-Agent, Signature-Input (web-bot-auth, @authority and signature-agent, keyid = the "
              "directory key's thumbprint, 60 s) and Signature", rc == 0 and hdrs and all(h.get("signature-agent") == '"https://agent.example"'
              and 'tag="web-bot-auth"' in h.get("signature-input", "") and f'keyid="{thumb}"' in h.get("signature-input", "")
              and '("@authority" "signature-agent")' in h.get("signature-input", "") and h.get("signature", "").startswith("sig1=:") for h in hdrs),
              hdrs[:1])
        check("wba directory --headers: the key directory and its signed response headers (tag http-message-signatures-directory)",
              d.get("url") == "https://agent.example/.well-known/http-message-signatures-directory" and d["directory"]["keys"][0]["kty"] == "OKP"
              and 'tag="http-message-signatures-directory"' in d.get("headers", {}).get("Signature-Input", ""), str(d)[:400])
        r = client("wba", "sign", "https://shop.example/x")
        check("wba sign gives signed headers for one site (60 s), recorded", r.returncode == 0 and "web-bot-auth" in r.stdout
              and any(x.get("op") == "wba_sign" and x.get("origin") == "https://shop.example" for x in audit_recs()), r.stdout[-300:])

    # --- 13. locking ends open purchases ----------------------------------------------------------------------------------
    with section('13. locking ends open purchases'):
        sflag("read-text", "Total $1.00")
        p = bg("pay", "visa", "--vm", "pvm", "--origin", SITE, "--total-selector", "#t")
        u.expect("Type visa to allow this one purchase", timeout=60)
        u.send("visa\n")
        rc, res, text = finish(p)
        u.send("\x03")
        u.wait(20)
        outputs.append(u.text())
        left13 = json.load(open(os.path.join(vdir, "purchases-active.json"))).get("purchases", []) if os.path.exists(os.path.join(vdir, "purchases-active.json")) else []
        check("locking the vault ends the open purchases (recorded as locked); the hub's file keeps only what a settle margin still "
              "covers (a minute at most, for purchases that tried a part), not the open purchase's own time", rc == 0
              and any(x.get("op") == "pay_end" and x.get("outcome") == "locked" and x.get("purchase") == display_id(res.get("purchase_id")) for x in audit_recs())
              and all(e.get("until", 0) <= time.time() + 62 for e in left13), left13)
        r = client("pay", "--purchase", res.get("purchase_id") or "0" * 32, "--vm", "pvm", "--field", "number")
        check("...and its key is no use afterwards", r.returncode == 3)

    # --- 14. F3: the spending record is never cut short; past its limit, purchases are refused ---------------------------
    with section('14. F3: the spending record is never cut short; past its lim'):
        u2 = start_unlock({"MCOMP_VAULT_TEST_LEDGER_MAX": "3"})
        before = spent("agentcard")
        sflag("read-text", "Total $1.00")
        r = client("pay", "agentcard", "--vm", "pvm", "--origin", SITE, "--total-selector", "#total")
        check("F3: a spending record at its limit refuses the next purchase instead of forgetting what still counts", r.returncode == 2
              and "spending record is full" in js(r.stdout).get("reason", "") and spent("agentcard") == before and before, (r.stdout[-300:], before))
        u2.send("\x03")
        u2.wait(20)
        outputs.append(u2.text())

    # --- no value anywhere -------------------------------------------------------------------------------------------------
    with section('no value anywhere'):
        recs = json.dumps(audit_recs())
        leaked = [v for v in VALUES if any(v in o for o in outputs) or v in recs]
        check(f"no card part or passphrase in any of {len(outputs)} outputs, the unlock terminal or the audit log", not leaked, leaked)
        check("the audit log's chain verifies (scripts/verify-audit.py)", subprocess.run([sys.executable, os.path.join(LAB, "scripts", "verify-audit.py"), alog],
                                                                                         capture_output=True).returncode == 0)

if __name__ == "__main__":
    rc = in_agent_session()
    sys.exit(main() if rc is None else rc)
