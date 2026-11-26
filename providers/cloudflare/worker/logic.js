// metacomputer's control Worker for a computer on Cloudflare Containers: the decisions, with no runtime import, so
// they're tested with node (tests/cloudflare_worker_test.mjs, run by tests/test_cloudflare.py). index.js is the
// runtime's part: the Worker's entry, the Durable Object that runs the computer's container, and the outbound handlers.
//
// Every request carries an HMAC-SHA256 of its method, path, query, time and body's hash, made on the Mac with the
// computer's own key (the Worker's secret MCOMP_KEY): verify() checks it in constant time (WebCrypto's verify), within
// SKEW_S of the Worker's clock. The key itself never travels.

export const ROUTES = new Set(["/v1/status", "/v1/start", "/v1/stop", "/v1/snapshot", "/v1/restore", "/v1/uncheckpoint",
  "/v1/net", "/v1/exec", "/v1/boot", "/v1/logs", "/v1/setup-done", "/v1/settings", "/v1/forget", "/v1/tcp"]);
export const SKEW_S = 300;
export const PORT = 2222;                              // the guest's sshd (Cloudflare's own SSH holds 22 when it's on)
export const SIZES = ["standard-2", "standard-3", "standard-4"];
export const MINUTE = 60000;
export const DAY = 86400000;
export const EXPIRE_MS = 30 * DAY;                     // a snapshot expires 30 days after it was made or last restored
export const REFRESH_MS = 25 * DAY;                    // ...so each one the computer keeps is restored before then
export const MAX_NAMES = 64;                           // hostnames a container's outbound handlers may name
export const SENTINEL = "metacomputer.invalid";        // registered (denied) at every start with the internet off: the
                                                       // runtime writes its interception CA only when one HTTPS name is
export const IDLE_MIN = 1, IDLE_MAX = 350;             // IDLE_STOP in minutes; the platform's own timeout is 6 h at most
export const BACKSTOP_MIN = 10;                        // the container's own inactivity timeout: IDLE_STOP + this
export const MAX_DROPPED = 200;
export const LABEL = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$/;
export const HOST = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
export const DENIED = "metacomputer: this computer's network doesn't let that name out\n";
export const DENIED_HOST = "metacomputer: the request asked for another name than the one allowed (its Host)\n";

const enc = new TextEncoder();

export class Refusal extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export const refuse = (status, message) => new Refusal(status, message);

export function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sha256hex(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes || new Uint8Array(0)));
}

export function canonical(method, path, query, ts, bodyHash) {
  return ["v1", method, path, query, ts, bodyHash].join("\n");
}

// The X-Mc-Auth header: v1.<unix seconds>.<64 hex digits>
export function parseAuth(h) {
  const m = /^v1\.([0-9]{9,12})\.([0-9a-f]{64})$/.exec(h || "");
  return m ? { ts: m[1], sig: m[2] } : null;
}

function unhex(s) {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

// {ok, why}: whether the request was signed with the computer's key. keyText is the Worker's secret (MCOMP_KEY): a
// Worker without one (or a short one) refuses everything.
export async function verify(keyText, method, url, header, body, nowMs) {
  if (typeof keyText !== "string" || keyText.length < 32) return { ok: false, why: "not configured" };
  const a = parseAuth(header);
  if (!a) return { ok: false, why: "no signature" };
  if (Math.abs(Math.floor(nowMs / 1000) - Number(a.ts)) > SKEW_S) return { ok: false, why: "stale" };
  const u = typeof url === "string" ? new URL(url) : url;
  const data = canonical(method, u.pathname, u.search.replace(/^\?/, ""), a.ts, await sha256hex(body));
  const key = await crypto.subtle.importKey("raw", enc.encode(keyText), { name: "HMAC", hash: "SHA-256" }, false,
    ["verify"]);
  const ok = await crypto.subtle.verify("HMAC", key, unhex(a.sig), enc.encode(data));
  return ok ? { ok: true } : { ok: false, why: "bad signature" };
}

// What a message may say: no account ID (a run of exactly 32 hex digits, as in an image's reference), no control
// characters, capped.
export function say(e, cap = 300) {
  const t = String((e && e.message) || e || "");
  return t.replace(/[\x00-\x1f\x7f]/g, " ").replace(/(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])/gi, "<account>").slice(0, cap);
}

// An outbound handler's host check: the request (whose URL the runtime builds from its Host header, not the TLS name
// it was intercepted on) must ask for exactly the allowed name, on the default port, over http or https.
export function hostAllowed(urlText, host) {
  if (typeof host !== "string" || !HOST.test(host)) return false;
  let u;
  try { u = new URL(urlText); } catch { return false; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  if (u.port !== "" || u.username || u.password) return false;
  return u.hostname.toLowerCase().replace(/\.$/, "") === host;
}

export function checkNames(list) {
  if (!Array.isArray(list)) throw refuse(400, "allow: a list of names");
  const out = [];
  for (const raw of list) {
    const n = String(raw).trim().toLowerCase().replace(/\.$/, "");
    if (!HOST.test(n) || n === SENTINEL) throw refuse(400, `${String(raw).slice(0, 80)} isn't a DNS name`);
    if (!out.includes(n)) out.push(n);
  }
  if (out.length > MAX_NAMES - 1) throw refuse(400, `an allowlist has at most ${MAX_NAMES - 1} names`);
  return out;
}

export function checkLabel(l) {
  if (typeof l !== "string" || !LABEL.test(l)) throw refuse(400, "a checkpoint's name: letters, digits, . _ -");
  return l;
}

export function checkIdle(m) {
  const n = Number(m);
  if (!Number.isInteger(n) || n < IDLE_MIN || n > IDLE_MAX) throw refuse(400, `IDLE_STOP: ${IDLE_MIN}-${IDLE_MAX} minutes`);
  return n;
}

export const backstopMs = (idleMin) => Math.min(360, idleMin + BACKSTOP_MIN) * MINUTE;

export function newRecord(env) {
  return {
    v: 1, computer: String(env.MCOMP_COMPUTER || ""), op: String(env.MCOMP_OP || ""),
    size: SIZES.includes(env.MCOMP_SIZE) ? env.MCOMP_SIZE : "standard-3", idle_min: 15,
    internet: true, allow: [], state: "never", latest: null, snaps: {}, checkpoints: {}, dropped: [],
    setup_done: false, ever_started: false, force: null, lost: null, last_activity: 0, idle_failures: 0,
  };
}

// The egress a computer asks for: internet (every name and address the platform lets out: not private ranges or the
// metadata service), off (nothing), or an allowlist of names (the internet off, those names through our handler).
export function wantNet(b) {
  if (b.allow !== undefined && b.allow !== null) {
    const allow = checkNames(b.allow);
    return { internet: false, allow };
  }
  if (b.egress === "internet") return { internet: true, allow: [] };
  if (b.egress === "off") return { internet: false, allow: [] };
  throw refuse(400, "egress: internet or off, or an allowlist");
}

export function egressWord(internet, allow) {
  return internet ? "internet" : (allow && allow.length ? "allowlist" : "off");
}

// How a running container gets from what's in force (force: {internet, names: {name: "allow"|"deny"}}) to what's
// wanted. Switching the internet on or off needs a new container (the platform sets it at start only); otherwise
// handlers are added or replaced live (one can't be removed while it runs: an allowed name taken off is replaced by
// the deny handler), unless the container would need more names than it may have.
export function planNet(force, want) {
  if (!force) return { restart: false, register: [], atStart: true };
  if (force.internet !== want.internet) return { restart: true, register: [] };
  if (want.internet) return { restart: false, register: [] };
  const names = force.names || {};
  const register = [];
  for (const n of want.allow) if (names[n] !== "allow") register.push([n, "allow"]);
  for (const [n, k] of Object.entries(names)) if (k === "allow" && !want.allow.includes(n)) register.push([n, "deny"]);
  const total = new Set([...Object.keys(names), ...want.allow, SENTINEL]).size;
  if (total > MAX_NAMES) return { restart: true, register: [] };
  return { restart: false, register };
}

// The intercepts a start registers before the container runs: none with the internet on; with it off, the sentinel
// (denied) and each allowed name.
export function startIntercepts(internet, allow) {
  if (internet) return [];
  return [[SENTINEL, "deny"], ...allow.map((n) => [n, "allow"])];
}

// --- snapshots -----------------------------------------------------------------------------------------------------
export function addSnap(r, s, now) {
  if (!s || typeof s.id !== "string" || !s.id) throw refuse(502, "the platform made a snapshot but didn't say its id");
  r.snaps[s.id] = { id: s.id, size: Number(s.size) || 0, made: now, fresh: now };
  return r.snaps[s.id];
}

export function keptIds(r) {
  const out = new Set();
  if (r.latest) out.add(r.latest);
  for (const id of Object.values(r.checkpoints || {})) out.add(id);
  return out;
}

// A snapshot nothing names any more leaves the record's care: it isn't refreshed, and expires 30 days after it was
// last made or restored (the platform can't delete one). It's remembered with that date, for status and vm delete.
export function dropUnused(r, id, now) {
  if (!id || keptIds(r).has(id) || !r.snaps[id]) return null;
  const s = r.snaps[id];
  delete r.snaps[id];
  const gone = { id, size: s.size, expires: s.fresh + EXPIRE_MS };
  r.dropped = [...(r.dropped || []).filter((d) => d.expires > now && d.id !== id), gone].slice(-MAX_DROPPED);
  return gone;
}

export function snapInfo(r, id) {
  const s = id && r.snaps[id];
  return s ? { id, size: s.size, made: s.made, fresh: s.fresh, expires: s.fresh + EXPIRE_MS, failed: s.failed || null } : null;
}

// Every snapshot of the computer that still exists at Cloudflare, as far as the record knows: those it keeps (their
// expiry 30 days after their last refresh) and those it let go that haven't expired.
export function residue(r, now) {
  const out = [];
  const labels = {};
  for (const [l, id] of Object.entries(r.checkpoints || {})) (labels[id] = labels[id] || []).push(l);
  if (r.latest) (labels[r.latest] = labels[r.latest] || []).push("latest");
  for (const id of keptIds(r)) {
    const s = r.snaps[id];
    if (s) out.push({ id, size: s.size, labels: (labels[id] || []).sort(), expires: s.fresh + EXPIRE_MS });
  }
  for (const d of r.dropped || []) if (d.expires > now) out.push({ id: d.id, size: d.size, labels: [], expires: d.expires });
  return out.sort((a, b) => a.expires - b.expires);
}

// The kept snapshots whose 30 days run out within the next five: each is started and ended at once (a restore resets
// its expiry), oldest first.
export function dueRefresh(r, now) {
  return [...keptIds(r)].map((id) => r.snaps[id]).filter((s) => s && !s.failed && now - s.fresh >= REFRESH_MS)
    .sort((a, b) => a.fresh - b.fresh).map((s) => s.id);
}

// --- the alarm ------------------------------------------------------------------------------------------------------
// Running: the idle stop (nothing connected for IDLE_STOP minutes: snapshot, then destroy), looked at every half of
// IDLE_STOP, a minute at most. Stopped: the next refresh due (a day at most between looks).
export function idleDecision(r, sockets, lastActivity, now) {
  const idleMs = r.idle_min * MINUTE;
  const every = Math.max(10000, Math.min(MINUTE, Math.floor(idleMs / 2)));
  if (sockets > 0) return { stop: false, next: now + every };
  return { stop: now - (lastActivity || 0) >= idleMs, next: now + every };
}

export function nextAlarm(r, running, now) {
  if (running) return now + Math.max(10000, Math.min(MINUTE, Math.floor(r.idle_min * MINUTE / 2)));
  const ids = [...keptIds(r)].map((id) => r.snaps[id]).filter((s) => s && !s.failed);
  if (!ids.length) return null;
  const due = Math.min(...ids.map((s) => s.fresh + REFRESH_MS));
  return Math.max(now + 1000, Math.min(due, now + DAY));
}

export function snapName(r, why, now) {
  const c = (r.computer || "computer").replace(/[^A-Za-z0-9-]/g, "-").slice(0, 40);
  return `${c}-${why}-${new Date(now).toISOString().replace(/[-:]/g, "").slice(0, 15)}`;
}
