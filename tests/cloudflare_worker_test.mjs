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
// The review's findings (audits/codex-cloudflare.md): their checks are tagged, in sections; a section that throws fails.
async function section(tag, fn) {
  try { await fn(); } catch (e) { check(`${tag} (the section threw: ${String(e && e.message || e).slice(0, 120)})`, false); }
}
const enc = new TextEncoder();
const hexOf = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const KEY = "k".repeat(64);

const newNonce = () => hexOf(crypto.getRandomValues(new Uint8Array(12)));
async function sign(key, method, url, body, ts, nonce = newNonce()) {
  const u = new URL(url);
  const b = body || new Uint8Array(0);
  const data = L.canonical(method, u.pathname, u.search.replace(/^\?/, ""), String(ts), nonce, b.length, await L.sha256hex(b));
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return `v1.${ts}.${nonce}.${hexOf(await crypto.subtle.sign("HMAC", k, enc.encode(data)))}`;
}
// X-Mc-Body for a body: its length and SHA-256 (signed with it).
const bodyHeader = async (body) => `${(body || new Uint8Array(0)).length}.${await L.sha256hex(body || new Uint8Array(0))}`;
// The body's length and hash as X-Mc-Body declares them (logic.js parseBody's answer), worked out here.
const declareOf = async (b) => ({ length: (b || new Uint8Array(0)).length, hash: await L.sha256hex(b || new Uint8Array(0)) });
// A signed request's headers, as the Mac sends them.
const signed = async (key, method, url, body, ts) => ({ "x-mc-auth": await sign(key, method, url, body, ts), "x-mc-body": await bodyHeader(body) });
// The header the entry passes on to the computer's object (the object checks only its nonce: the entry checked the rest).
const doAuth = () => `v1.${Math.floor(Date.now() / 1000)}.${newNonce()}.${"0".repeat(64)}`;

// --- logic.js: the signature ------------------------------------------------------------------------------------------
await section("the signature", async () => {
  const now = Date.now(), ts = Math.floor(now / 1000), url = "https://w.example.workers.dev/v1/status";
  const body = enc.encode("{}");
  const h = await sign(KEY, "POST", url, body, ts);
  const dec = await declareOf(body);
  check("verify: a request signed with the key passes (from its headers: the body's length and hash declared)", (await L.verify(KEY, "POST", url, h, dec, now)).ok);
  check("...with another key: refused", !(await L.verify("x".repeat(64), "POST", url, h, dec, now)).ok);
  check("...with another body declared: refused", !(await L.verify(KEY, "POST", url, h, await declareOf(enc.encode("{\"a\":1}")), now)).ok);
  check("...on another path: refused", !(await L.verify(KEY, "POST", "https://w.example.workers.dev/v1/stop", h, dec, now)).ok);
  check("...with a query added: refused", !(await L.verify(KEY, "POST", url + "?x=1", h, dec, now)).ok);
  check("...as another method: refused", !(await L.verify(KEY, "GET", url, h, dec, now)).ok);
  check("...301 s old: refused (stale)", (await L.verify(KEY, "POST", url, await sign(KEY, "POST", url, body, ts - 301), dec, now)).why === "stale");
  check("...299 s old: passes", (await L.verify(KEY, "POST", url, await sign(KEY, "POST", url, body, ts - 299), dec, now)).ok);
  check("...no header, or a malformed one: refused", !(await L.verify(KEY, "POST", url, null, dec, now)).ok
        && !(await L.verify(KEY, "POST", url, "v1." + ts + ".zz", dec, now)).ok
        && !(await L.verify(KEY, "POST", url, "Bearer " + KEY, dec, now)).ok);
  await section("[replay]", async () => {
    check("[replay] verify: the nonce is signed: the same signature with another nonce is refused", (await L.verify(KEY, "POST", url, h, dec, now)).ok
          && !(await L.verify(KEY, "POST", url, h.replace(/^(v1\.[0-9]+\.)[0-9a-f]{24}/, "$1" + "0".repeat(24)), dec, now)).ok);
  });
  await section("[P2-body]", async () => {
    check("[P2-body] preAuth: the header's shape and time, checked without the body", L.preAuth(KEY, h, now).ok
          && L.preAuth(KEY, "v1." + ts + ".zz", now).why === "no signature" && L.preAuth(KEY, null, now).why === "no signature"
          && L.preAuth(KEY, await sign(KEY, "POST", url, body, ts - 301), now).why === "stale"
          && L.preAuth("", h, now).why === "not configured");
  });
  check("...a Worker without a key (or a short one) refuses everything", (await L.verify("", "POST", url, h, dec, now)).why === "not configured"
        && (await L.verify("short", "POST", url, h, dec, now)).why === "not configured"
        && (await L.verify(undefined, "POST", url, h, dec, now)).why === "not configured");
  check("...the signature is checked with WebCrypto's verify (constant time), never compared as text",
        /crypto\.subtle\.verify\("HMAC"/.test((await import("node:fs")).readFileSync(path.join(W, "logic.js"), "utf8")));
});

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
  await section("[P1]", async () => {
    const due = t + L.REFRESH_MS + 5000;
    L.refreshFailed(r.snaps.s2, "no capacity", due);
    check("[P1] ...one whose refresh failed: not tried again before its back-off (5 min after the first failure)",
          JSON.stringify(L.dueRefresh(r, due + 1000)) === '["s3"]' && r.snaps.s2.retry_at === due + 5 * L.MINUTE);
    check("[P1] ...then tried again (a failed refresh is never given up on before the snapshot expires)",
          JSON.stringify(L.dueRefresh(r, due + 5 * L.MINUTE)) === '["s2","s3"]');
    L.refreshFailed(r.snaps.s2, "no capacity", due + 5 * L.MINUTE);
    L.refreshFailed(r.snaps.s2, "no capacity", due + 15 * L.MINUTE);
    check("[P1] ...the back-off doubles (5, 10, 20 min), 6 h at most", r.snaps.s2.retry_at === due + 35 * L.MINUTE
          && L.retryDelay(20) === 6 * 3600000 && r.snaps.s2.refresh_failures === 3);
    check("[P1] ...the alarm stays set for its next try (not given up on)", L.nextAlarm({ ...r, checkpoints: { baseline: "s2" }, latest: null }, false, due + 16 * L.MINUTE) === due + 35 * L.MINUTE);
    const tr = L.refreshTrouble(r, due + 16 * L.MINUTE);
    check("[P1] refreshTrouble: a failing refresh said with its names, its failures, its last error and its expiry",
          tr.length === 1 && tr[0].id === "s2" && tr[0].labels.join() === "baseline" && tr[0].failures === 3
          && tr[0].error === "no capacity" && tr[0].expires === r.snaps.s2.fresh + L.EXPIRE_MS && !tr[0].expired);
    check("[P1] ...and in its snapshot's info", L.snapInfo(r, "s2").refresh.failures === 3);
    check("[P1] ...one that expired anyway: said as expired, not tried again, no alarm for it",
          L.refreshTrouble(r, t + L.EXPIRE_MS + 2000).some((x) => x.id === "s2" && x.expired)
          && !L.dueRefresh(r, t + L.EXPIRE_MS + 2000).includes("s2"));
    L.refreshDone(r.snaps.s2, t + 1000);
    check("[P1] refreshDone: renewed, the failures forgotten", r.snaps.s2.fresh === t + 1000 && !r.snaps.s2.refresh_failures
          && !r.snaps.s2.retry_at && L.snapInfo(r, "s2").refresh === null);
  });
  r.snaps.s2.fresh = t + 1000;
  for (const k of ["refresh_failures", "refresh_error", "retry_at", "failed"]) delete r.snaps.s2[k];
  check("nextAlarm: stopped, the next refresh due (a day at most between looks)", L.nextAlarm(r, false, t + 3000) === t + 3000 + L.DAY);
  check("...due sooner than a day: then", L.nextAlarm(r, false, t + L.REFRESH_MS - 1000) === t + 1000 + L.REFRESH_MS);
  check("...nothing kept: no alarm", L.nextAlarm(L.newRecord({}), false, t) === null);
  r.idle_min = 15;
  check("...running: every half of IDLE_STOP, a minute at most", L.nextAlarm(r, true, t) === t + L.MINUTE);
  r.idle_min = 1;
  check("...IDLE_STOP=1: every 30 s (two looks within it)", L.nextAlarm(r, true, t) === t + 30000);
  check("[P1] nextAlarm, running after three failed idle stops: the refresh's look still (a day at most), not none",
        L.nextAlarm({ ...r, idle_failures: 3 }, true, t + 3000) === t + 3000 + L.DAY);
  check("[P2-stop] nextAlarm: a stop whose container didn't end: tried again within a minute",
        L.nextAlarm({ ...r, state: "stopping", stop_pending: { tries: 1 } }, false, t + 3000) === t + 3000 + L.MINUTE);
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
  const env = { MCOMP_KEY: KEY, COMPUTER: { idFromName: (n) => n, get: () => ({ claim: async () => ({ ok: true }),
    fetch: async (u, init) => { seen.push([String(u.url || u), init && init.method]); return new Response("{}"); } }) } };
  const base = "https://w.example.workers.dev";
  const now = Math.floor(Date.now() / 1000);
  const post = async (p, key = KEY, body = "{}") => I.default.fetch(new Request(base + p, {
    method: "POST", body, headers: await signed(key, "POST", base + p, enc.encode(body), now) }), env);
  check("the entry: an unsigned request is refused (401) before the computer sees it",
        (await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: "{}" }), env)).status === 401 && seen.length === 0);
  check("...one signed with another key: 401", (await post("/v1/status", "z".repeat(64))).status === 401 && seen.length === 0);
  check("...a signed request on a path that isn't one: 404 (after the signature)", (await post("/v1/nope")).status === 404 && seen.length === 0);
  check("...a GET that isn't a websocket: 401", (await I.default.fetch(new Request(base + "/v1/status"), env)).status === 401);
  check("...a signed one reaches the computer's object", (await post("/v1/status")).status === 200 && seen.length === 1);
  check("...the tcp route only as a websocket", (await post("/v1/tcp")).status === 404);
  const noKey = { ...env, MCOMP_KEY: undefined };
  check("...a Worker without its key refuses everything", (await I.default.fetch(new Request(base + "/v1/status", {
    method: "POST", body: "{}", headers: await signed(KEY, "POST", base + "/v1/status", enc.encode("{}"), now) }), noKey)).status === 401);
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
  constructor() { this.running = false; this.images = { ubuntu: "registry.cloudflare.com/" + "0".repeat(32) + "/repo@sha256:" + "a".repeat(64) }; this.log = []; this.n = 0; this.failSnapshot = false; this.execCode = 0; this.names = {}; this.failStart = 0; this.failDestroy = 0; }
  start(opts) {
    if (this.failStart > 0) { this.failStart--; this.log.push(["start-failed", opts]); throw new Error("no capacity in the region"); }
    this.running = true; this.opts = opts; this.log.push(["start", opts]);
  }
  async destroy() {
    if (this.failDestroy > 0) { this.failDestroy--; this.log.push(["destroy-failed"]); throw new Error("internal error"); }
    this.running = false; this.names = {}; this.log.push(["destroy"]);
  }
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
function makeObject(c, env, idText) {
  const store = new Map();
  const ctx = {
    id: { toString: () => idText }, container: c,
    exports: { Allow: (o) => ({ kind: "allow", props: o.props }), Deny: { kind: "deny" } },
    storage: { get: async (k) => structuredClone(store.get(k)), put: async (k, v) => { store.set(k, structuredClone(v)); },
               list: async (o) => new Map([...store].filter(([k]) => k.startsWith(o.prefix)).slice(0, o.limit)),
               delete: async (keys) => { for (const k of [].concat(keys)) store.delete(k); },
               deleteAll: async () => store.clear(), setAlarm: async (t) => { ctx.alarm = t; }, deleteAlarm: async () => { ctx.alarm = null; } },
    blockConcurrencyWhile: (fn) => fn(), alarm: null,
  };
  const o = new I.Computer(ctx, env);
  o.ctx = ctx;
  return { o, ctx, store };
}
// The computer's object, and the refresher (an object of the same class with a container of its own) behind the
// Worker's namespace binding, as the runtime gives both.
function makeDO(c, rc = new FakeContainer()) {
  const objects = {};
  const env = { MCOMP_COMPUTER: "box", MCOMP_OP: "op1", MCOMP_SIZE: "standard-3",
                COMPUTER: { idFromName: (n) => n, get: (id) => objects[id] } };
  const { o: d, ctx, store } = makeObject(c, env, "ab".repeat(32));
  const ref = makeObject(rc, env, "cd".repeat(32));
  objects.computer = d;
  objects.refresher = ref.o;
  d.refresher = ref.o;
  d.rc = rc;
  d.idleSince = (ms) => { d.lastActivity = ms; const r = structuredClone(store.get("rec")); r.last_activity = ms; store.set("rec", r); };
  d.call = async (verb, body = {}, auth = doAuth()) => {
    const r = await d.fetch(new Request("https://computer/v1/" + verb, { method: "POST", body: JSON.stringify(body),
      headers: { "x-mc-auth": auth } }));
    return { status: r.status, body: await r.json() };
  };
  d.age = (days, which) => {                       // the record's snapshots made (and last renewed) `days` ago
    const r = structuredClone(store.get("rec"));
    for (const id of Object.keys(r.snaps)) if (!which || which.includes(id)) { r.snaps[id].fresh = Date.now() - days * L.DAY; r.snaps[id].made = r.snaps[id].fresh; }
    store.set("rec", r);
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
  d.age(26);
  const starts = c.log.filter((x) => x[0] === "start").length;
  await d.alarm();
  const lastStart = (d.rc.log.filter((x) => x[0] === "start").at(-1) || [null, {}])[1];
  check("alarm, stopped, a kept snapshot 26 days old: started by the refresher (the internet off) and destroyed at once, its expiry renewed",
        c.log.filter((x) => x[0] === "start").length === starts && !d.rc.running && lastStart.enableInternet === false
        && (lastStart.containerSnapshot || {}).id === (await d.call("status")).body.latest.id
        && Date.now() - (await d.call("status")).body.latest.fresh < 60000);
  check("...the computer stays stopped", (await d.call("status")).body.state === "stopped" && !c.running);
  r = await d.call("net", { egress: "internet" });
  check("net while stopped: recorded, applied at the next start", r.status === 200 && r.body.at_next_start === true);
  r = await d.call("settings", { idle_min: 351 });
  check("settings: IDLE_STOP past 350: refused", r.status === 400);
  r = await d.call("forget");
  check("forget: the residue said (each snapshot with its date), the record and the alarm gone", r.status === 200
        && r.body.residue.length >= 2 && r.body.residue.every((x) => x.expires > Date.now()) && !store.has("rec") && ctx.alarm === null);
  check("[N2-tomb] ...a tombstone left, and every request after it refused (410)", store.has("tombstone")
        && (await d.call("status")).status === 410 && (await d.call("start", { from_image: true })).status === 410 && !c.running);
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

// --- The review's findings (audits/codex-cloudflare.md) ------------------------------------------------------------------
const realNow = Date.now;
let skew = 0;
Date.now = () => realNow() + skew;
const atStart = (c) => c.log.filter((x) => x[0] === "start");

// [P1] A refresh that fails once (no capacity) is tried again after its back-off, never given up on, and said.
await section("[P1]", async () => {
  skew = 0;
  const c = new FakeContainer(), rc = new FakeContainer();
  const { d, ctx } = makeDO(c, rc);
  await d.call("start", { from_image: true, boot: false });
  await d.call("stop");
  d.age(26);
  c.failStart = 1; rc.failStart = 1;
  await d.alarm();
  c.failStart = 0;
  let st = (await d.call("status")).body;
  check("[P1] a refresh that fails (no capacity): recorded with its error, the snapshot still kept, said in status",
        Array.isArray(st.refresh) && st.refresh.length === 1 && st.refresh[0].failures === 1 && /no capacity/.test(st.refresh[0].error)
        && st.refresh[0].labels.join() === "latest" && !st.refresh[0].expired && st.latest.refresh && st.latest.refresh.failures === 1, st.refresh);
  check("[P1] ...the alarm still set, for its next try (5 min on)", typeof ctx.alarm === "number"
        && ctx.alarm > Date.now() + 4 * L.MINUTE && ctx.alarm <= Date.now() + 5 * L.MINUTE + 2000, ctx.alarm - Date.now());
  await d.alarm();
  check("[P1] ...not tried again before its back-off", atStart(rc).length === 0);
  skew += 5 * L.MINUTE + 1000;
  await d.alarm();
  st = (await d.call("status")).body;
  check("[P1] ...tried again after it, once Cloudflare has room: renewed, the failure forgotten",
        atStart(rc).length === 1 && st.refresh.length === 0 && Date.now() - st.latest.fresh < 60000 && !rc.running, st.refresh);
  rc.failStart = 100;
  d.age(26);
  for (let i = 0; i < 6; i++) { skew += 6 * 3600000; await d.alarm(); }
  st = (await d.call("status")).body;
  check("[P1] a refresh that keeps failing: tried again and again (6 tries in 36 h), said each time, the alarm kept",
        st.refresh.length === 1 && st.refresh[0].failures >= 6 && typeof ctx.alarm === "number" && !st.refresh[0].expired, st.refresh);
  skew += 4 * L.DAY;
  st = (await d.call("status")).body;
  check("[P1] ...one that expired all the same: said as expired", st.refresh.length === 1 && st.refresh[0].expired === true);
  skew = 0;
});

// [P2-running] Kept snapshots of a computer that keeps running (a connection open) are renewed without stopping it.
await section("[P2-running]", async () => {
  skew = 0;
  const c = new FakeContainer(), rc = new FakeContainer();
  const { d } = makeDO(c, rc);
  await d.call("start", { from_image: true, boot: false });
  await d.call("stop");
  await d.call("start", { boot: false });
  const base = (await d.call("snapshot", { label: "baseline" })).body.snapshot.id;
  const latest = (await d.call("status")).body.latest.id;
  d.sockets = 1;
  d.idleSince(Date.now());
  d.age(26);
  const n0 = c.n, starts0 = atStart(c).length;
  await d.alarm();
  await d.alarm();
  let st = (await d.call("status")).body;
  check("[P2-running] a connection open, the baseline and the latest snapshot 26 days old: both renewed by the refresher",
        Date.now() - st.checkpoints.baseline.fresh < 60000 && Date.now() - st.latest.fresh < 60000
        && atStart(rc).map((x) => x[1].containerSnapshot.id).sort().join() === [base, latest].sort().join(), atStart(rc).length);
  check("[P2-running] ...the computer kept running throughout: never stopped, snapshotted or started again",
        c.running && c.n === n0 && atStart(c).length === starts0 && st.state === "running");
  skew += 5 * L.DAY;                               // the baseline made 31 days ago
  const r = await d.call("restore", { label: "baseline" });
  check("[P2-running] ...31 days after the baseline was made, it still restores (not 410)", r.status === 200
        && c.opts.containerSnapshot.id === base, r);
  skew = 0;
});

// [P2-stop] A stop whose container Cloudflare doesn't end: said as it is, tried again soon, and a start isn't blocked.
await section("[P2-stop]", async () => {
  skew = 0;
  const c = new FakeContainer(), rc = new FakeContainer();
  const { d, ctx } = makeDO(c, rc);
  await d.call("start", { from_image: true, boot: false });
  c.failDestroy = 1;
  let r = await d.call("stop");
  let st = (await d.call("status")).body;
  check("[P2-stop] destroy fails after the snapshot: the stop answers an error (not success) that says the disk was saved and it still runs",
        r.status >= 500 && /disk was saved/.test(r.body.error) && /still runs/.test(r.body.error) && /nothing is lost/.test(r.body.error), r);
  check("[P2-stop] ...recorded as it is: stopping, still running, the snapshot its latest", st.state === "stopping" && st.running === true
        && st.latest && st.latest.id === "snap-1" && st.stop_pending && st.stop_pending.tries === 1, st);
  check("[P2-stop] ...the alarm set to try again within a minute (not a day)", typeof ctx.alarm === "number" && ctx.alarm - Date.now() <= 61000);
  await d.alarm();
  st = (await d.call("status")).body;
  check("[P2-stop] ...the next alarm ends it: stopped, by the stop, nothing lost", st.state === "stopped" && !c.running
        && st.stop_reason === "stop" && !st.lost && !st.stop_pending, st);
  // A start while it's stopping: the container ended then, and started from the stop's snapshot.
  await d.call("start", { boot: false });
  c.failDestroy = 1;
  await d.call("stop");
  r = await d.call("start", { boot: true });
  check("[P2-stop] a start while stopping: the container ended, started again from the stop's snapshot (no 409)",
        r.status === 200 && c.opts.containerSnapshot.id === (await d.call("status")).body.latest.id
        && (await d.call("status")).body.state === "running", r);
  // A start while it's stopping and Cloudflare still won't end it: taken up again as it is.
  c.failDestroy = 2;
  await d.call("stop");
  const starts = atStart(c).length;
  r = await d.call("start", { boot: true });
  st = (await d.call("status")).body;
  check("[P2-stop] ...and when Cloudflare still won't end it: taken up again as it is (resumed, booted), not refused",
        r.status === 200 && r.body.resumed === true && st.state === "running" && c.running && atStart(c).length === starts
        && c.log.at(-1)[0] === "exec", r);
  // A start while stopping whose egress changed to one that needs a new container: refused, said.
  c.failDestroy = 2;
  await d.call("stop");
  await d.call("net", { egress: "off" });
  r = await d.call("start", { boot: false });
  check("[P2-stop] ...unless the egress asked for since needs a new container: an error saying the disk is saved",
        r.status >= 500 && /disk was saved/.test(r.body.error) && (await d.call("status")).body.state === "stopping", r);
  c.failDestroy = 0;
  await d.alarm();
  check("[P2-stop] ...then the alarm ends it", (await d.call("status")).body.state === "stopped" && !c.running);
  // A restore whose destroy fails changes nothing.
  await d.call("start", { boot: false });
  await d.call("snapshot", { label: "ck" });
  const before = (await d.call("status")).body.latest.id;
  c.failDestroy = 1;
  r = await d.call("restore", { label: "ck" });
  check("[P2-stop] a restore whose destroy fails: an error, nothing changed", r.status >= 500
        && (await d.call("status")).body.latest.id === before && (await d.call("status")).body.state === "running", r);
});

// [P2-body], [N1-body] The entry reads no body before its signature (over the body's declared length and hash) and its
// nonce have passed, never past the length signed, and refuses a body that isn't the one signed.
await section("[P2-body]", async () => {
  skew = 0;
  const base = "https://w.example.workers.dev";
  const seen = [], claimed = [];
  const env = { MCOMP_KEY: KEY, COMPUTER: { idFromName: (n) => n, get: () => ({ claim: async (h) => { claimed.push(h); return { ok: true }; },
    fetch: async (u, init) => { seen.push([String(u.url || u), init && init.body]); return new Response("{}"); } }) } };
  const counting = (chunks, size = 1 << 16) => {
    const st = { pulled: 0, bytes: 0 };
    st.stream = new ReadableStream({ pull(ctrl) { st.pulled++; st.bytes += size; ctrl.enqueue(new Uint8Array(size)); if (st.pulled >= chunks) ctrl.close(); } }, { highWaterMark: 0 });
    return st;
  };
  const ts = Math.floor(Date.now() / 1000);
  let b = counting(1000);
  let res = await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: b.stream, duplex: "half" }), env);
  check("[P2-body] an unsigned POST: 401, and not a byte of its body read", res.status === 401 && b.pulled === 0, b.pulled);
  b = counting(1000);
  res = await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: b.stream, duplex: "half",
    headers: await signed(KEY, "POST", base + "/v1/status", new Uint8Array(0), ts - 400) }), env);
  check("[P2-body] ...a stale signature: 401, nothing read", res.status === 401 && b.pulled === 0, b.pulled);
  // The review's case: a made-up header of the right shape, on /v1/exec, with 72 MB behind it.
  const forged = `v1.${ts}.${"0".repeat(24)}.${"1".repeat(64)}`;
  b = counting(1152);
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: b.stream, duplex: "half",
    headers: { "x-mc-auth": forged, "x-mc-body": `${72 << 20}.${"2".repeat(64)}` } }), env);
  check("[N1-body] a forged header of the right shape on /v1/exec, 72 MB behind it: 401 before a byte is read, no nonce claimed",
        res.status === 401 && b.bytes === 0 && claimed.length === 0 && seen.length === 0, [res.status, b.bytes]);
  b = counting(1152);
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: b.stream, duplex: "half",
    headers: { "x-mc-auth": forged } }), env);
  check("[N1-body] ...the same without its body's length and hash: 401, nothing read", res.status === 401 && b.bytes === 0, b.bytes);
  b = counting(1000);
  res = await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: b.stream, duplex: "half",
    headers: { "x-mc-auth": forged, "x-mc-body": `${100000000}.${"2".repeat(64)}` } }), env);
  check("[P2-body] ...a length past the route's cap: 413, nothing read", res.status === 413 && b.pulled === 0, b.pulled);
  // Signed correctly, but the body sent isn't the one signed.
  const body = enc.encode(JSON.stringify({ argv: ["id"] }));
  const tampered = enc.encode(JSON.stringify({ argv: ["sh"] }));
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: tampered,
    headers: await signed(KEY, "POST", base + "/v1/exec", body, ts) }), env);
  check("[N1-body] a valid signature with a tampered body (same length): refused (400) once read, the computer never sees it",
        res.status === 400 && /isn't the one signed/.test((await res.json()).error) && seen.length === 0 && claimed.length === 1, res.status);
  b = counting(40);
  const sig16 = new Uint8Array(1 << 16);
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: b.stream, duplex: "half",
    headers: await signed(KEY, "POST", base + "/v1/exec", sig16, ts) }), env);
  check("[N1-body] ...a body longer than the one signed: 400, reading stopped just past the length signed",
        res.status === 400 && b.pulled === 2 && seen.length === 0, [res.status, b.pulled]);
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: sig16.subarray(0, 100),
    headers: await signed(KEY, "POST", base + "/v1/exec", sig16, ts) }), env);
  check("[N1-body] ...shorter than signed: 400", res.status === 400 && seen.length === 0, res.status);
  const big = new Uint8Array(1 << 20).fill(7);
  res = await I.default.fetch(new Request(base + "/v1/exec", { method: "POST", body: big,
    headers: await signed(KEY, "POST", base + "/v1/exec", big, ts) }), env);
  check("[P2-body] ...the setup's exec still takes a large signed body (1 MB), whole", res.status === 200 && seen.length === 1
        && seen[0][1].length === big.length && seen[0][1].every((x) => x === 7), res.status);
  const small = enc.encode("{}");
  res = await I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: small,
    headers: await signed(KEY, "POST", base + "/v1/status", small, ts) }), env);
  check("[P2-body] ...and a signed request within its cap reaches the computer", res.status === 200 && seen.length === 2);
  // readSigned on its own: one buffer of the signed length, hashed as it comes with the runtime's DigestStream.
  const { createHash } = await import("node:crypto");
  let wrote = 0;
  class DS {
    constructor() { const h = createHash("sha256"); let done; this.digest = new Promise((r) => { done = r; });
      this.w = { write: async (c) => { wrote += c.length; h.update(c); }, close: async () => done(h.digest().buffer) }; }
    getWriter() { return this.w; }
  }
  const data = new Uint8Array(200000).fill(3);
  const st = new ReadableStream({ start(c) { for (let i = 0; i < 200000; i += 50000) c.enqueue(data.subarray(i, i + 50000)); c.close(); } });
  const got = await L.readSigned(st, await declareOf(data), DS);
  check("[N1-body] readSigned: hashed as it comes (the runtime's DigestStream), into one buffer of the length signed",
        got.ok && got.body.length === 200000 && wrote === 200000 && got.body.buffer.byteLength === 200000);
  const st2 = new ReadableStream({ start(c) { c.enqueue(data); c.close(); } });
  check("[N1-body] ...and with no DigestStream (one digest of that buffer): the same",
        (await L.readSigned(st2, await declareOf(data))).ok && !(await L.readSigned(new ReadableStream({ start(c) { c.enqueue(data); c.close(); } }), await declareOf(tampered))).ok);
});

// [replay] A signed request can't be sent again: the computer's object remembers its nonce while it could be fresh.
await section("[replay]", async () => {
  skew = 0;
  const base = "https://w.example.workers.dev";
  const c = new FakeContainer();
  const { d, ctx, store } = makeDO(c);
  // The runtime hands the object a Request, whatever the stub's fetch was given; claim is its RPC method.
  const stubOf = (o) => ({ fetch: (u, init) => o.fetch(u instanceof Request ? u : new Request(u, init)), claim: (h) => o.claim(h) });
  const env = { MCOMP_KEY: KEY, COMPUTER: { idFromName: (n) => n, get: () => stubOf(d) } };
  const body = enc.encode(JSON.stringify({ from_image: true, boot: false }));
  const ts = Math.floor(Date.now() / 1000);
  const hs = await signed(KEY, "POST", base + "/v1/start", body, ts);
  const send = () => I.default.fetch(new Request(base + "/v1/start", { method: "POST", body, headers: hs }), env);
  const r1 = await send();
  await d.call("stop");
  const r2 = await send();
  check("[replay] the same signed request sent again (a start replayed after a stop, within its 300 s): refused, nothing done",
        r1.status === 200 && r2.status === 401 && !c.running, [r1.status, r2.status]);
  const again = new I.Computer(ctx, { MCOMP_COMPUTER: "box", MCOMP_OP: "op1", COMPUTER: env.COMPUTER });
  const r3 = await I.default.fetch(new Request(base + "/v1/start", { method: "POST", body, headers: hs }),
    { ...env, COMPUTER: { idFromName: (n) => n, get: () => stubOf(again) } });
  check("[replay] ...also after the object restarted (the nonce kept in its storage)", r3.status === 401 && !c.running, r3.status);
  const fresh = await signed(KEY, "POST", base + "/v1/status", enc.encode("{}"), ts);
  const statusWith = (h) => I.default.fetch(new Request(base + "/v1/status", { method: "POST", body: "{}", headers: h }), env);
  const two = await Promise.all([statusWith(fresh), statusWith(fresh)]);
  check("[replay] ...two copies of a new one at the same moment: exactly one passes", two.filter((x) => x.status === 200).length === 1
        && two.filter((x) => x.status === 401).length === 1, two.map((x) => x.status));
  const h2 = await signed(KEY, "POST", base + "/v1/status", enc.encode("{}"), ts);
  const h3 = await signed(KEY, "POST", base + "/v1/status", enc.encode("{}"), ts);
  check("[replay] identical requests in the same second, each with its own nonce: both pass", h2["x-mc-auth"] !== h3["x-mc-auth"]
        && (await statusWith(h2)).status === 200 && (await statusWith(h3)).status === 200);
  const nonces = () => [...store.keys()].filter((k) => k.startsWith("n:")).length;
  const had = nonces();
  skew += 10 * L.MINUTE;
  for (let i = 0; i < 64; i++) await statusWith(await signed(KEY, "POST", base + "/v1/status", enc.encode("{}"), Math.floor(Date.now() / 1000)));
  check("[replay] nonces are forgotten once their requests would be stale anyway", had > 0 && nonces() <= 64, [had, nonces()]);
  skew = 0;
});
// [N2-tomb] A delete cut off after the Worker's forget (its deletion failed, the object restarted within the requests'
// 300 s) can't let a request it saw through again: the tombstone refuses every request, whatever its nonce.
await section("[N2-tomb]", async () => {
  skew = 0;
  const base = "https://w.example.workers.dev";
  const c = new FakeContainer();
  const { d, ctx, store } = makeDO(c);
  const stubOf = (o) => ({ fetch: (u, init) => o.fetch(u instanceof Request ? u : new Request(u, init)), claim: (h) => o.claim(h) });
  const envOf = (o) => ({ MCOMP_KEY: KEY, COMPUTER: { idFromName: (n) => n, get: () => stubOf(o) } });
  const body = enc.encode(JSON.stringify({ from_image: true, boot: false }));
  const ts = Math.floor(Date.now() / 1000);
  const hs = await signed(KEY, "POST", base + "/v1/start", body, ts);
  const startWith = (o, h) => I.default.fetch(new Request(base + "/v1/start", { method: "POST", body, headers: h }), envOf(o));
  const r1 = await startWith(d, hs);
  const fh = await signed(KEY, "POST", base + "/v1/forget", enc.encode("{}"), ts);
  const r2 = await I.default.fetch(new Request(base + "/v1/forget", { method: "POST", body: "{}", headers: fh }), envOf(d));
  const reloaded = new I.Computer(ctx, { MCOMP_COMPUTER: "box", MCOMP_OP: "op1" });
  const r3 = await startWith(reloaded, hs);
  check("[N2-tomb] forget, the object reloaded, then the start it saw sent again: refused (410), nothing started",
        r1.status === 200 && r2.status === 200 && r3.status === 410 && !c.running && store.has("tombstone"), [r1.status, r2.status, r3.status]);
  const r4 = await startWith(reloaded, await signed(KEY, "POST", base + "/v1/start", body, ts));
  const r5 = await I.default.fetch(new Request(base + "/v1/tcp", { headers: { upgrade: "websocket",
    "x-mc-auth": await sign(KEY, "GET", base + "/v1/tcp", new Uint8Array(0), ts) } }), envOf(reloaded));
  check("[N2-tomb] ...and a new request with a new nonce too, and a new SSH connection: 410", r4.status === 410 && r5.status === 410
        && !c.running, [r4.status, r5.status]);
  await reloaded.alarm();
  check("[N2-tomb] ...its alarm does nothing (no record, no refresh)", !c.running && !store.has("rec") && ctx.alarm === null);
  // The same at the object itself (the review's reproduction): a request it took, forget, a reload, the request again.
  const c2 = new FakeContainer();
  const m = makeDO(c2);
  const seenAuth = doAuth();
  const a1 = await m.d.call("start", { from_image: true, boot: false }, seenAuth);
  await m.d.call("forget");
  const back = new I.Computer(m.ctx, { MCOMP_COMPUTER: "box", MCOMP_OP: "op1" });
  const res = await back.fetch(new Request("https://computer/v1/start", { method: "POST",
    body: JSON.stringify({ from_image: true, boot: false }), headers: { "x-mc-auth": seenAuth } }));
  check("[N2-tomb] at the object: a start it took, then forget, a reload and the same start again: refused (410), nothing started",
        a1.status === 200 && res.status === 410 && !c2.running, [a1.status, res.status, c2.running]);
});
Date.now = realNow;

console.log(`${passes} passed, ${fails} failed`);
process.exit(Math.min(fails, 100));
