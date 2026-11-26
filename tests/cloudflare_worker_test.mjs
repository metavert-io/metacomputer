// The control Worker of a computer on Cloudflare (providers/cloudflare/worker/), run with node: its pure logic
// (logic.js) and its runtime part (index.js) against a stand-in for `cloudflare:workers` and a fake container. No
// network, no Cloudflare account. Run by tests/test_cloudflare.py (node 18 or later), or by hand:
//     node tests/cloudflare_worker_test.mjs
// Each check prints PASS or FAIL; the exit status is the number of failures (capped at 100).
import { register } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const W = path.join(HERE, "..", "providers", "cloudflare", "worker");
const STUB = `export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }
export class WorkerEntrypoint { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }`;
const HOOKS = `export async function resolve(spec, ctx, next) {
  if (spec === "cloudflare:workers") return { url: "data:text/javascript," + encodeURIComponent(${JSON.stringify(STUB)}), shortCircuit: true };
  return next(spec, ctx);
}`;
register("data:text/javascript," + encodeURIComponent(HOOKS));

const L = await import(pathToFileURL(path.join(W, "logic.js")).href);
globalThis.scheduler = { wait: (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 5))) };
const I = await import(pathToFileURL(path.join(W, "index.js")).href);

let fails = 0, passes = 0;
function check(name, cond, detail) {
  if (cond) { passes++; console.log("PASS " + name); }
  else { fails++; console.log("FAIL " + name + (detail !== undefined ? "  (" + JSON.stringify(detail) + ")" : "")); }
}
async function rejects(p) { try { await p; return null; } catch (e) { return e; } }
const enc = new TextEncoder();
const hexOf = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const KEY = "k".repeat(64);

async function sign(key, method, url, body, ts) {
  const u = new URL(url);
  const data = L.canonical(method, u.pathname, u.search.replace(/^\?/, ""), String(ts), await L.sha256hex(body));
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return `v1.${ts}.${hexOf(await crypto.subtle.sign("HMAC", k, enc.encode(data)))}`;
}

// --- logic.js: the signature ------------------------------------------------------------------------------------------
{
  const now = Date.now(), ts = Math.floor(now / 1000), url = "https://w.example.workers.dev/v1/status";
  const body = enc.encode("{}");
  const h = await sign(KEY, "POST", url, body, ts);
  check("verify: a request signed with the key passes", (await L.verify(KEY, "POST", url, h, body, now)).ok);
  check("...with another key: refused", !(await L.verify("x".repeat(64), "POST", url, h, body, now)).ok);
  check("...with another body: refused", !(await L.verify(KEY, "POST", url, h, enc.encode("{\"a\":1}"), now)).ok);
  check("...on another path: refused", !(await L.verify(KEY, "POST", "https://w.example.workers.dev/v1/stop", h, body, now)).ok);
  check("...with a query added: refused", !(await L.verify(KEY, "POST", url + "?x=1", h, body, now)).ok);
  check("...as another method: refused", !(await L.verify(KEY, "GET", url, h, body, now)).ok);
  check("...301 s old: refused (stale)", (await L.verify(KEY, "POST", url, await sign(KEY, "POST", url, body, ts - 301), body, now)).why === "stale");
  check("...299 s old: passes", (await L.verify(KEY, "POST", url, await sign(KEY, "POST", url, body, ts - 299), body, now)).ok);
  check("...no header, or a malformed one: refused", !(await L.verify(KEY, "POST", url, null, body, now)).ok
        && !(await L.verify(KEY, "POST", url, "v1." + ts + ".zz", body, now)).ok
        && !(await L.verify(KEY, "POST", url, "Bearer " + KEY, body, now)).ok);
  check("...a Worker without a key (or a short one) refuses everything", (await L.verify("", "POST", url, h, body, now)).why === "not configured"
        && (await L.verify("short", "POST", url, h, body, now)).why === "not configured"
        && (await L.verify(undefined, "POST", url, h, body, now)).why === "not configured");
  check("...the signature is checked with WebCrypto's verify (constant time), never compared as text",
        /crypto\.subtle\.verify\("HMAC"/.test((await import("node:fs")).readFileSync(path.join(W, "logic.js"), "utf8")));
}

// --- logic.js: the host check of an allowlist's handler -----------------------------------------------------------
check("hostAllowed: the allowed name over https and http", L.hostAllowed("https://example.com/x?y", "example.com")
      && L.hostAllowed("http://example.com/", "example.com"));
check("...its name in capitals or with a trailing dot (the URL's own reading)", L.hostAllowed("https://EXAMPLE.com./", "example.com"));
check("...another Host than the name it was intercepted on: refused (the spike reached IANA this way)",
      !L.hostAllowed("https://www.iana.org/", "example.com"));
check("...a subdomain, a suffix, a port, credentials, another scheme: refused",
      !L.hostAllowed("https://a.example.com/", "example.com") && !L.hostAllowed("https://notexample.com/", "example.com")
      && !L.hostAllowed("https://example.com:8443/", "example.com") && !L.hostAllowed("https://u:p@example.com/", "example.com")
      && !L.hostAllowed("ftp://example.com/", "example.com") && !L.hostAllowed("not a url", "example.com"));
check("...a handler without its name refuses everything", !L.hostAllowed("https://example.com/", undefined)
      && !L.hostAllowed("https://example.com/", ""));

// --- logic.js: names, egress plans ------------------------------------------------------------------------------------
check("checkNames: lowercased, deduplicated, trailing dot dropped", JSON.stringify(L.checkNames(["Example.COM.", "example.com", "b.org"])) === '["example.com","b.org"]');
check("...an address, a wildcard, the sentinel or junk: refused", ["1.2.3.4", "*.example.com", "metacomputer.invalid", "a b", "localhost"]
      .every((n) => { try { L.checkNames([n]); return false; } catch (e) { return e.status === 400; } }));
check("...at most 63 names (the sentinel takes the 64th)", (() => { try { L.checkNames([...Array(64)].map((_, i) => `n${i}.example.com`)); return false; } catch { return true; } })()
      && L.checkNames([...Array(63)].map((_, i) => `n${i}.example.com`)).length === 63);
check("wantNet: internet, off, an allowlist (the internet off)", JSON.stringify(L.wantNet({ egress: "internet" })) === '{"internet":true,"allow":[]}'
      && JSON.stringify(L.wantNet({ egress: "off" })) === '{"internet":false,"allow":[]}'
      && JSON.stringify(L.wantNet({ allow: ["a.org"] })) === '{"internet":false,"allow":["a.org"]}');
check("...anything else refused", !!(() => { try { L.wantNet({ egress: "open" }); } catch (e) { return e; } })());
{
  const force = (internet, names) => ({ internet, names });
  const p1 = L.planNet(force(true, {}), { internet: false, allow: [] });
  check("planNet: internet to off restarts (the platform sets it at start only)", p1.restart === true);
  const p2 = L.planNet(force(false, { "metacomputer.invalid": "deny" }), { internet: true, allow: [] });
  check("...off to internet restarts", p2.restart === true);
  const p3 = L.planNet(force(false, { "metacomputer.invalid": "deny" }), { internet: false, allow: ["a.org"] });
  check("...off to an allowlist: live, the name's handler added", !p3.restart && JSON.stringify(p3.register) === '[["a.org","allow"]]');
  const p4 = L.planNet(force(false, { "metacomputer.invalid": "deny", "a.org": "allow", "b.org": "allow" }), { internet: false, allow: ["b.org"] });
  check("...a name taken off: live, its handler replaced by Deny (one can't be removed while it runs)",
        !p4.restart && JSON.stringify(p4.register) === '[["a.org","deny"]]');
  const p5 = L.planNet(force(false, { "metacomputer.invalid": "deny", "a.org": "deny" }), { internet: false, allow: ["a.org"] });
  check("...a name denied earlier, allowed again: live, Allow again", !p5.restart && JSON.stringify(p5.register) === '[["a.org","allow"]]');
  const many = Object.fromEntries([...Array(63)].map((_, i) => [`n${i}.org`, "deny"]));
  const p6 = L.planNet(force(false, { ...many, "metacomputer.invalid": "deny" }), { internet: false, allow: ["new.org"] });
  check("...more than 64 names on the container: a restart instead", p6.restart === true);
  check("...stopped: nothing in force, applied at its next start", L.planNet(null, { internet: false, allow: [] }).atStart === true);
  check("startIntercepts: none with the internet on; the sentinel (denied) and each name with it off",
        L.startIntercepts(true, ["a.org"]).length === 0
        && JSON.stringify(L.startIntercepts(false, ["a.org"])) === '[["metacomputer.invalid","deny"],["a.org","allow"]]'
        && JSON.stringify(L.startIntercepts(false, [])) === '[["metacomputer.invalid","deny"]]');
}

// --- logic.js: snapshots, the alarm -----------------------------------------------------------------------------------
{
  const r = L.newRecord({ MCOMP_COMPUTER: "box", MCOMP_OP: "op1", MCOMP_SIZE: "standard-4" });
  check("newRecord: the computer, its operation and size from the Worker's bindings; the internet on, never started",
        r.computer === "box" && r.op === "op1" && r.size === "standard-4" && r.internet === true && r.state === "never");
  check("...an unknown size falls back to standard-3", L.newRecord({ MCOMP_SIZE: "huge" }).size === "standard-3");
  const t = 1_800_000_000_000;
  L.addSnap(r, { id: "s1", size: 10 }, t);
  r.latest = "s1";
  L.addSnap(r, { id: "s2", size: 20 }, t + 1000);
  r.checkpoints.baseline = "s2";
  check("keptIds: the latest and every checkpoint", [...L.keptIds(r)].sort().join() === "s1,s2");
  L.addSnap(r, { id: "s3", size: 30 }, t + 2000);
  r.latest = "s3";
  const gone = L.dropUnused(r, "s1", t + 2000);
  check("dropUnused: a snapshot nothing names leaves the record, remembered with its expiry (30 days after its last use)",
        !r.snaps.s1 && gone && gone.expires === t + L.EXPIRE_MS && r.dropped.some((d) => d.id === "s1"));
  check("...one a checkpoint still names stays", L.dropUnused(r, "s2", t) === null && !!r.snaps.s2);
  const res = L.residue(r, t + 3000);
  check("residue: every snapshot still at Cloudflare (kept ones and ones let go that haven't expired), each with its date",
        res.length === 3 && res.every((x) => x.expires > t) && res.find((x) => x.id === "s2").labels.join() === "baseline"
        && res.find((x) => x.id === "s3").labels.join() === "latest");
  check("...one let go that has expired isn't listed", L.residue(r, t + L.EXPIRE_MS + 1).every((x) => x.id !== "s1"));
  check("dueRefresh: nothing due before 25 days", L.dueRefresh(r, t + L.REFRESH_MS - 10).length === 0);
  check("...each kept one due at 25 days, oldest first", JSON.stringify(L.dueRefresh(r, t + L.REFRESH_MS + 5000)) === '["s2","s3"]');
  r.snaps.s2.failed = "gone";
  check("...one whose refresh failed isn't tried again", JSON.stringify(L.dueRefresh(r, t + L.REFRESH_MS + 5000)) === '["s3"]');
  delete r.snaps.s2.failed;
  check("nextAlarm: stopped, the next refresh due (a day at most between looks)", L.nextAlarm(r, false, t + 3000) === t + 3000 + L.DAY);
  check("...due sooner than a day: then", L.nextAlarm(r, false, t + L.REFRESH_MS - 1000) === t + 1000 + L.REFRESH_MS);
  check("...nothing kept: no alarm", L.nextAlarm(L.newRecord({}), false, t) === null);
  r.idle_min = 15;
  check("...running: every half of IDLE_STOP, a minute at most", L.nextAlarm(r, true, t) === t + L.MINUTE);
  r.idle_min = 1;
  check("...IDLE_STOP=1: every 30 s (two looks within it)", L.nextAlarm(r, true, t) === t + 30000);
  check("idleDecision: a connection open: never stopped", L.idleDecision(r, 1, 0, t).stop === false);
  check("...none open, IDLE_STOP since the last: stopped", L.idleDecision(r, 0, t - 60000, t).stop === true);
  check("...none open, less than IDLE_STOP: not yet", L.idleDecision(r, 0, t - 59000, t).stop === false);
  check("backstopMs: IDLE_STOP + 10 minutes, 6 hours at most", L.backstopMs(15) === 25 * L.MINUTE && L.backstopMs(350) === 360 * L.MINUTE);
  check("checkIdle: 1-350 minutes", L.checkIdle(1) === 1 && L.checkIdle(350) === 350
        && [0, 351, 1.5, "x"].every((v) => { try { L.checkIdle(v); return false; } catch { return true; } }));
  check("say: an account's ID (32 hex digits, as in an image's reference) never in a message",
        !L.say(new Error(`image registry.cloudflare.com/${"0123456789abcdef".repeat(2)}/x not found`)).includes("0123456789abcdef".repeat(2)));
}

// --- index.js: one start path ---------------------------------------------------------------------------------------
{
  const src = (await import("node:fs")).readFileSync(path.join(W, "index.js"), "utf8");
  check("index.js starts a container in one place only (launch), which applies the recorded egress before it runs",
        (src.match(/this\.c\.start\(/g) || []).length === 1 && /async launch\(r, source, refresh = false\)/.test(src)
        && src.indexOf("L.startIntercepts(internet, r.allow)") < src.indexOf("this.c.start(opts)"));
  check("...and passes the container no environment (no key, no account's ID, no binding)", !/opts\.env|env: /.test(src.slice(src.indexOf("async launch"), src.indexOf("async run("))));
}

// --- index.js: the entry ---------------------------------------------------------------------------------------------
{
  const seen = [];
  const env = { MCOMP_KEY: KEY, COMPUTER: { idFromName: (n) => n, get: () => ({ fetch: async (u, init) => { seen.push([String(u.url || u), init && init.method]); return new Response("{}"); } }) } };
  const base = "https://w.example.workers.dev";
  const now = Math.floor(Date.now() / 1000);
  const post = async (p, key = KEY, body = "{}") => I.default.fetch(new Request(base + p, {
    method: "POST", body, headers: { "x-mc-auth": await sign(key, "POST", base + p, enc.encode(body), now) } }), env);
  check("the entry: an unsigned request is refused (401) before the computer sees it",
        (await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: "{}" }), env)).status === 401 && seen.length === 0);
  check("...one signed with another key: 401", (await post("/v1/status", "z".repeat(64))).status === 401 && seen.length === 0);
  check("...a signed request on a path that isn't one: 404 (after the signature)", (await post("/v1/nope")).status === 404 && seen.length === 0);
  check("...a GET that isn't a websocket: 401", (await I.default.fetch(new Request(base + "/v1/status"), env)).status === 401);
  check("...a signed one reaches the computer's object", (await post("/v1/status")).status === 200 && seen.length === 1);
  check("...the tcp route only as a websocket", (await post("/v1/tcp")).status === 404);
  const noKey = { ...env, MCOMP_KEY: undefined };
  check("...a Worker without its key refuses everything", (await I.default.fetch(new Request(base + "/v1/status", {
    method: "POST", body: "{}", headers: { "x-mc-auth": await sign(KEY, "POST", base + "/v1/status", enc.encode("{}"), now) } }), noKey)).status === 401);
}

// --- index.js: the outbound handlers ---------------------------------------------------------------------------------
{
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push([url, init]); return new Response("ok", { status: 200 }); };
  const allow = new I.Allow({ props: { host: "example.com" } }, {});
  const r1 = await allow.fetch(new Request("https://example.com/a?b=1", { method: "GET" }));
  check("Allow: the allowed name is fetched, never following a redirect", r1.status === 200 && calls.length === 1
        && calls[0][0] === "https://example.com/a?b=1" && calls[0][1].redirect === "manual");
  const r2 = await allow.fetch(new Request("https://www.iana.org/", { method: "GET" }));
  check("...a request for another name than its own (the Host header): 403, nothing fetched", r2.status === 403 && calls.length === 1);
  const r3 = await new I.Allow({ props: {} }, {}).fetch(new Request("https://example.com/"));
  check("...a handler made without its name: 403", r3.status === 403 && calls.length === 1);
  const r4 = await new I.Deny({}, {}).fetch(new Request("https://example.com/"));
  check("Deny: 403, nothing fetched", r4.status === 403 && calls.length === 1);
}

// --- index.js: the computer, against a fake container -----------------------------------------------------------------
class FakeContainer {
  constructor() { this.running = false; this.images = { ubuntu: "registry.cloudflare.com/" + "0".repeat(32) + "/repo@sha256:" + "a".repeat(64) }; this.log = []; this.n = 0; this.failSnapshot = false; this.execCode = 0; this.names = {}; }
  start(opts) { this.running = true; this.opts = opts; this.log.push(["start", opts]); }
  async destroy() { this.running = false; this.names = {}; this.log.push(["destroy"]); }
  async setInactivityTimeout(ms) { this.timeout = ms; }
  async interceptOutboundHttps(n, h) { this.names[n] = h.kind; this.log.push(["https", n, h.kind, h.props && h.props.host]); }
  async interceptOutboundHttp(n, h) { this.log.push(["http", n, h.kind]); }
  async snapshotContainer(o) { if (this.failSnapshot) throw new Error("snapshot failed"); this.n++; this.log.push(["snapshot", o.name]); return { id: "snap-" + this.n, size: 1000 + this.n }; }
  async exec(argv, opts) {
    this.log.push(["exec", argv, !!(opts && opts.stdin)]);
    const code = argv[0] === "true" ? 0 : this.execCode;
    return { exitCode: Promise.resolve(code), output: async () => ({ exitCode: code, stdout: enc.encode("out"), stderr: new Uint8Array(0) }), kill() {} };
  }
  ops() { return this.log.map((x) => x[0]); }
}
function makeDO(c) {
  const store = new Map();
  const ctx = {
    id: { toString: () => "ab".repeat(32) }, container: c,
    exports: { Allow: (o) => ({ kind: "allow", props: o.props }), Deny: { kind: "deny" } },
    storage: { get: async (k) => structuredClone(store.get(k)), put: async (k, v) => { store.set(k, structuredClone(v)); },
               deleteAll: async () => store.clear(), setAlarm: async (t) => { ctx.alarm = t; }, deleteAlarm: async () => { ctx.alarm = null; } },
    blockConcurrencyWhile: (fn) => fn(), alarm: null,
  };
  const d = new I.Computer(ctx, { MCOMP_COMPUTER: "box", MCOMP_OP: "op1", MCOMP_SIZE: "standard-3" });
  d.ctx = ctx;
  d.idleSince = (ms) => { d.lastActivity = ms; const r = structuredClone(store.get("rec")); r.last_activity = ms; store.set("rec", r); };
  d.call = async (verb, body = {}) => {
    const r = await d.fetch(new Request("https://computer/v1/" + verb, { method: "POST", body: JSON.stringify(body) }));
    return { status: r.status, body: await r.json() };
  };
  return { d, ctx, store };
}
{
  const c = new FakeContainer();
  const { d, ctx, store } = makeDO(c);
  let r = await d.call("status");
  check("status: a new computer is never started, internet set, nothing kept", r.status === 200 && r.body.state === "never"
        && r.body.egress.setting === "internet" && r.body.do === "ab".repeat(32) && r.body.computer === "box" && r.body.op === "op1");
  check("...its image's reference (it names the account) is never in an answer", !JSON.stringify(r.body).includes("0".repeat(32)));
  r = await d.call("start", { boot: false });
  check("start: nothing to start from, and not asked to start from the image: refused (409)", r.status === 409 && !c.running);
  r = await d.call("start", { from_image: true, boot: false, idle_min: 20 });
  check("start from the image: the container runs, the internet on, no handler registered, its size and entrypoint",
        r.status === 200 && c.running && c.opts.enableInternet === true && c.opts.image && !c.opts.containerSnapshot
        && c.opts.instance === "standard-3" && c.ops().every((o) => o !== "https") && c.opts.entrypoint[0] === "/bin/sh", r.body);
  check("...the container's own inactivity timeout set: IDLE_STOP + 10 minutes", c.timeout === 30 * L.MINUTE);
  check("...an alarm set (the idle stop)", typeof ctx.alarm === "number");
  r = await d.call("exec", { argv: ["bash", "-c", "x"], stdin_b64: btoa("hi") });
  check("exec, before the setup's end: run as asked, its standard input passed", r.status === 200 && r.body.code === 0
        && c.log.at(-1)[0] === "exec" && c.log.at(-1)[2] === true);
  r = await d.call("exec", { argv: "bash -c x" });
  check("...a command that isn't a list of words: refused", r.status === 400);
  r = await d.call("net", { allow: ["example.com"] });
  check("net from the internet to an allowlist, running: a restart (snapshot, destroy, start with the internet off)",
        r.status === 200 && r.body.restarted === true && c.opts.enableInternet === false && c.opts.containerSnapshot.id === "snap-1",
        [r.body, c.ops()]);
  check("...registered before the start: the sentinel (denied) and the name (allowed, with its own name as its props)",
        c.log.some((x) => x[0] === "https" && x[1] === "metacomputer.invalid" && x[2] === "deny")
        && c.log.some((x) => x[0] === "https" && x[1] === "example.com" && x[2] === "allow" && x[3] === "example.com")
        && c.log.findIndex((x) => x[0] === "https" && x[1] === "example.com") < c.log.findLastIndex((x) => x[0] === "start"));
  const before = c.log.length;
  r = await d.call("net", { egress: "off" });
  check("net from the allowlist to off: live, no restart, the name's handler replaced by Deny", r.status === 200 && !r.body.restarted
        && c.names["example.com"] === "deny" && !c.log.slice(before).some((x) => x[0] === "start" || x[0] === "snapshot"), [r.body, c.log.slice(before)]);
  r = await d.call("status");
  check("...status says off, in force", r.body.egress.setting === "off" && r.body.egress.in_force.names["example.com"] === "deny");
  await d.call("setup-done");
  r = await d.call("exec", { argv: ["id"] });
  check("exec after the setup's end: refused (403): the key runs no command any more", r.status === 403);
  r = await d.call("boot", {});
  check("...the boot script is still a request of its own", r.status === 200 && c.log.at(-1)[1][0] === "/opt/metacomputer/cloudflare/boot");
  c.execCode = 1;
  r = await d.call("boot", { restart_desktop: true });
  check("...a boot that fails: an error, said", r.status === 502 && /boot script failed/.test(r.body.error));
  c.execCode = 0;
  r = await d.call("snapshot", { label: "ck1" });
  check("checkpoint: a live snapshot, the container keeps running", r.status === 200 && c.running && r.body.snapshot.id === "snap-2");
  r = await d.call("snapshot", { label: "ck1" });
  check("...again under the same name without replace: refused", r.status === 409);
  r = await d.call("snapshot", { label: "ck1", replace: true });
  check("...with replace: the old one let go, with its expiry (Cloudflare can't delete one)", r.status === 200
        && r.body.let_go && r.body.let_go.id === "snap-2" && r.body.let_go.expires > Date.now());
  r = await d.call("snapshot", { label: "../x" });
  check("...a name that isn't one: refused", r.status === 400);
  c.failSnapshot = true;
  r = await d.call("stop");
  check("stop with a snapshot that fails: nothing is stopped (the container keeps running)", r.status >= 500 && c.running
        && (await d.call("status")).body.state === "running");
  c.failSnapshot = false;
  const quiet = c.log.length;
  r = await d.call("stop");
  check("stop: the desktop's programs ended, the disk snapshotted, then the container destroyed, in that order",
        r.status === 200 && !c.running && JSON.stringify(c.ops().slice(quiet)) === '["exec","snapshot","destroy"]'
        && c.log[quiet][1].join(" ") === "/opt/metacomputer/cloudflare/boot quiesce", c.log.slice(quiet));
  r = await d.call("status");
  check("...stopped, the new snapshot its latest, kept until 30 days from now", r.body.state === "stopped" && r.body.latest.id === "snap-4"
        && r.body.latest.expires > Date.now() + 29 * L.DAY);
  check("...an alarm for the refresh (stopped)", typeof ctx.alarm === "number" && ctx.alarm > Date.now());
  r = await d.call("start", { boot: true });
  check("start: from its latest snapshot, the egress recorded (off: the internet off, the sentinel), then the boot script",
        r.status === 200 && c.opts.containerSnapshot.id === "snap-4" && c.opts.enableInternet === false
        && c.names["metacomputer.invalid"] === "deny" && !("example.com" in c.names) && c.log.at(-1)[1][0] === "/opt/metacomputer/cloudflare/boot");
  r = await d.call("restore", { label: "ck1" });
  check("restore: what runs is destroyed without a snapshot, the container starts from the checkpoint and boots",
        r.status === 200 && c.opts.containerSnapshot.id === "snap-3" && c.n === 4);
  r = await d.call("status");
  check("...the checkpoint is its latest now; the snapshot it replaced as latest let go", r.body.latest.id === "snap-3"
        && r.body.residue.some((x) => x.id === "snap-4" && x.labels.length === 0));
  r = await d.call("uncheckpoint", { label: "ck1" });
  check("uncheckpoint: forgotten; still the latest, so kept", r.status === 200 && !r.body.let_go);
  r = await d.call("restore", { label: "nope" });
  check("restore of a checkpoint there isn't: 404", r.status === 404);
  // The alarm: the idle stop.
  d.sockets = 1;
  d.idleSince(Date.now() - 3600_000);
  let n0 = c.n;
  await d.alarm();
  check("alarm: a connection open: never stopped, whatever the time", c.running && c.n === n0);
  d.sockets = 0;
  d.lastActivity = Date.now();
  await d.alarm();
  check("...the later of the two activity times counts (one in memory, one stored): recent, not stopped", c.running && c.n === n0);
  d.sockets = 0;
  d.idleSince(Date.now() - 10 * L.MINUTE);
  await d.alarm();
  check("...none open for less than IDLE_STOP (20 min): not stopped", c.running && c.n === n0);
  d.idleSince(Date.now() - 21 * L.MINUTE);
  await d.alarm();
  check("...none open for IDLE_STOP: a snapshot, then stopped", !c.running && c.n === n0 + 1
        && (await d.call("status")).body.stop_reason === "idle");
  // A stop by Cloudflare (the container gone while the record says it runs).
  await d.call("start", { boot: false });
  c.running = false;
  r = await d.call("status");
  check("a stop by Cloudflare: said as a stop, with what changed since the last snapshot lost", r.body.state === "stopped"
        && r.body.stop_reason === "cloudflare" && r.body.lost && r.body.lost.since > 0);
  // The refresh.
  const rec = structuredClone(store.get("rec"));
  for (const id of Object.keys(rec.snaps)) rec.snaps[id].fresh = Date.now() - 26 * L.DAY;
  store.set("rec", rec);
  const starts = c.log.filter((x) => x[0] === "start").length;
  await d.alarm();
  const lastStart = c.log.filter((x) => x[0] === "start").at(-1)[1];
  check("alarm, stopped, a kept snapshot 26 days old: started (the internet off) and destroyed at once, its expiry renewed",
        c.log.filter((x) => x[0] === "start").length === starts + 1 && !c.running && lastStart.enableInternet === false
        && Date.now() - (await d.call("status")).body.latest.fresh < 60000);
  check("...the computer stays stopped", (await d.call("status")).body.state === "stopped");
  r = await d.call("net", { egress: "internet" });
  check("net while stopped: recorded, applied at the next start", r.status === 200 && r.body.at_next_start === true);
  r = await d.call("forget");
  check("forget: the residue said (each snapshot with its date), the storage and the alarm gone", r.status === 200
        && r.body.residue.length >= 2 && r.body.residue.every((x) => x.expires > Date.now()) && store.size === 0 && ctx.alarm === null);
  check("...after it, a new record (never started)", (await d.call("status")).body.state === "never");
  r = await d.call("settings", { idle_min: 351 });
  check("settings: IDLE_STOP past 350: refused", r.status === 400);
}
// A status asked while a stop runs (outside the lock): the container is gone before the stop records it, which must not
// be taken for a stop by Cloudflare.
{
  const c = new FakeContainer();
  const { d } = makeDO(c);
  await d.call("start", { from_image: true, boot: false });
  let release;
  const gate = new Promise((r) => { release = r; });
  const realDestroy = c.destroy.bind(c);
  c.destroy = async () => { c.running = false; await gate; return realDestroy(); };
  const stopping = d.call("stop");
  await new Promise((r) => setTimeout(r, 20));
  const mid = await d.call("status");
  release();
  await stopping;
  const after = await d.call("status");
  check("a status while a stop runs: not a stop by Cloudflare (nothing lost), and the stop's own record stands",
        !mid.body.lost && after.body.stop_reason === "stop" && !after.body.lost && after.body.state === "stopped", [mid.body.lost, after.body]);
}
// The snapshot failures in the idle stop: three tries, then no more (the container's own timeout ends it).
{
  const c = new FakeContainer();
  const { d, ctx } = makeDO(c);
  await d.call("start", { from_image: true, boot: false, idle_min: 1 });
  c.failSnapshot = true;
  d.sockets = 0;
  d.idleSince(Date.now() - 5 * L.MINUTE);
  await d.alarm(); await d.alarm();
  check("idle stop with snapshots failing: tried again at the next alarm", typeof ctx.alarm === "number" && c.running);
  ctx.alarm = null;
  await d.alarm();
  check("...after three failures no alarm is set again (the platform's inactivity timeout, under it, ends it)",
        ctx.alarm === null && c.running && (await d.call("status")).body.state === "running");
}

console.log(`${passes} passed, ${fails} failed`);
process.exit(Math.min(fails, 100));
