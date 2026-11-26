// metacomputer's control Worker for one computer on Cloudflare Containers (docs/cloudflare.md). metacomputer deploys
// it into the user's own account, one per computer, on workers.dev only (no route on any of the account's domains):
// providers/cloudflare/api.py uploads this file and logic.js through Cloudflare's API.
//
// - The entry (default): every request must be signed with the computer's own key (logic.js verify, in constant
//   time, over its headers: the body's length and hash are signed in X-Mc-Body); anything else is refused (401)
//   before it reaches the computer and before a byte of its body is read. The computer's object then claims the
//   request's nonce (a request seen before is refused, 401; after the computer's forget, every request is, 410), and
//   only then is the body read: to the length signed at most (its route's cap, 413 past it), and refused (400) unless
//   it's the one signed (logic.js readSigned). Only the routes in logic.js ROUTES exist.
// - Computer: one Durable Object, the computer's, which runs its container (the durable_object scheduling policy):
//   start (its latest snapshot, or the image the first time), stop (its disk snapshotted, then the container
//   destroyed: a stop keeps files, not programs), checkpoints, egress, SSH (a websocket piped to the container's
//   sshd), the idle stop and the refresh that keeps its snapshots from expiring (the alarm). A second object of the
//   class, the refresher (never reachable from outside: the entry sends everything to the computer's), restores each
//   snapshot due in a container of its own and ends it at once, so a running computer's are renewed too.
// - Allow and Deny: the outbound handlers of an allowlist (the internet off, each allowed name intercepted): Allow
//   checks the request asks for exactly its name (the runtime builds the URL from the Host header, not the TLS name)
//   and fetches only that name, never following a redirect; Deny answers 403.
// Nothing here puts the key, the account's IDs or an image's reference (it names the account) in an answer.
import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";
import * as L from "./logic.js";

const BOOT = "/opt/metacomputer/cloudflare/boot";
// PID 1 of every start: ends on SIGTERM (a platform stop's grace period), so nothing waits on it.
const ENTRY = ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"];
const MAX_OUT = 4 << 20;
const SNAP_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

const json = (o, status = 200, headers = {}) =>
  new Response(JSON.stringify(o), { status, headers: { "content-type": "application/json", ...headers } });

function b64(bytes) {
  const u = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || 0);
  let s = "";
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000));
  return btoa(s);
}
function unb64(t) {
  const s = atob(t);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const ws = (req.headers.get("upgrade") || "").toLowerCase() === "websocket";
    if (req.method !== (ws ? "GET" : "POST")) return json({ error: "unauthorized" }, 401);
    const header = req.headers.get("x-mc-auth");
    // Nothing of the body is read before its signature and its nonce have passed: the signature covers the body's
    // length and hash, which travel in a header.
    if (!L.preAuth(env.MCOMP_KEY, header, Date.now()).ok) return json({ error: "unauthorized" }, 401);
    const declared = ws ? L.NO_BODY : L.parseBody(req.headers.get("x-mc-body"));
    if (!declared) return json({ error: "unauthorized" }, 401);
    if (declared.length > L.bodyCap(url.pathname)) return json({ error: "too large" }, 413);
    const auth = await L.verify(env.MCOMP_KEY, req.method, url, header, declared, Date.now());
    if (!auth.ok) return json({ error: "unauthorized" }, 401);
    if (!L.ROUTES.has(url.pathname) || (url.pathname === "/v1/tcp") !== ws) return json({ error: "no such request" }, 404);
    const stub = env.COMPUTER.get(env.COMPUTER.idFromName("computer"));
    const claim = await stub.claim(header);
    if (!claim || !claim.ok) return json({ error: (claim && claim.why) || "unauthorized" }, (claim && claim.status) || 401);
    if (ws) return stub.fetch(req);
    const got = await L.readSigned(req.body, declared, globalThis.crypto && globalThis.crypto.DigestStream);
    if (!got.ok) return json({ error: got.why }, got.status);
    return stub.fetch("https://computer" + url.pathname, { method: "POST", body: got.body,
      headers: { "content-type": "application/json" } });
  },
};

export class Allow extends WorkerEntrypoint {
  async fetch(req) {
    const host = this.ctx.props && this.ctx.props.host;
    if (!L.hostAllowed(req.url, host)) return new Response(L.DENIED_HOST, { status: 403 });
    const u = new URL(req.url);
    const init = { method: req.method, headers: req.headers, redirect: "manual" };
    if (req.method !== "GET" && req.method !== "HEAD") init.body = req.body;
    return fetch(`${u.protocol}//${host}${u.pathname}${u.search}`, init);
  }
}

export class Deny extends WorkerEntrypoint {
  async fetch() {
    return new Response(L.DENIED, { status: 403 });
  }
}

export class Computer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sockets = 0;
    this.lastActivity = 0;
    this.busy = 0;
    this.chain = Promise.resolve();
    this.seen = new Map();                                       // nonces of requests seen: nonce -> when to forget it
    this.noted = 0;
    ctx.blockConcurrencyWhile(async () => {
      const r = await ctx.storage.get("rec");
      this.lastActivity = (r && r.last_activity) || 0;
      // The container's own inactivity timeout (the backstop under the idle stop) is set again whenever the object
      // starts again.
      if (r && ctx.container && ctx.container.running) {
        try { await ctx.container.setInactivityTimeout(L.backstopMs(r.idle_min)); } catch {}
      }
    });
  }

  // One change at a time (start, stop, snapshots, egress, the alarm): each waits for the one before.
  lock(fn) {
    const run = async () => {
      this.busy++;
      try { return await fn(); } finally { this.busy--; }
    };
    const p = this.chain.then(run, run);
    this.chain = p.then(() => {}, () => {});
    return p;
  }

  async rec() {
    return (await this.ctx.storage.get("rec")) || L.newRecord(this.env);
  }

  async save(r) {
    r.last_activity = Math.max(r.last_activity || 0, this.lastActivity || 0);
    await this.ctx.storage.put("rec", r);
  }

  get c() {
    return this.ctx.container;
  }

  // A request seen before (its nonce) is refused: a signed request can't be sent again, not even within its time.
  // Checked in memory first (at once, so two copies at the same moment can't both pass), then in storage (an object
  // that restarted in between).
  async once(header) {
    const a = L.parseAuth(header);
    if (!a) return false;
    const now = Date.now();
    if (this.seen.has(a.nonce)) return false;
    this.seen.set(a.nonce, L.nonceExpiry(a.ts));
    if (await this.ctx.storage.get("n:" + a.nonce)) return false;
    await this.ctx.storage.put("n:" + a.nonce, L.nonceExpiry(a.ts));
    if (++this.noted % 64 === 0 || this.seen.size > 512) {
      for (const [n, t] of this.seen) if (t < now) this.seen.delete(n);
      try {
        const old = [...(await this.ctx.storage.list({ prefix: "n:", limit: 1000 })).entries()]
          .filter(([, t]) => t < now).map(([k]) => k);
        for (let i = 0; i < old.length; i += 128) await this.ctx.storage.delete(old.slice(i, i + 128));
      } catch {}
    }
    return true;
  }

  // The entry's call for each signed request, before its body is read: its nonce claimed (never twice), or, once the
  // computer has been forgotten (vm delete), every request refused for good (the tombstone outlives a restart, so a
  // delete cut off before the Worker goes can't reopen the requests it saw). A plain answer: RPC keeps no error's status.
  async claim(header) {
    if (await this.ctx.storage.get("tombstone")) return { ok: false, status: 410, why: "this computer was deleted" };
    if (!(await this.once(header))) return { ok: false, status: 401, why: "unauthorized (a request sent again)" };
    return { ok: true };
  }

  async fetch(req) {
    const p = new URL(req.url).pathname;
    if (!this.c) return json({ error: "no container binding" }, 500);
    if (await this.ctx.storage.get("tombstone")) return json({ error: "this computer was deleted" }, 410);
    if (p === "/v1/tcp") return this.tcp(req);
    let b = {};
    try {
      const t = await req.text();
      b = t ? JSON.parse(t) : {};
      if (!b || typeof b !== "object" || Array.isArray(b)) throw new Error("not an object");
    } catch {
      return json({ error: "not a request" }, 400);
    }
    try {
      switch (p) {
        case "/v1/status": return json(await this.status());
        case "/v1/start": return json(await this.lock(() => this.start(b)));
        case "/v1/stop": return json(await this.lock(() => this.stopVerb()));
        case "/v1/snapshot": return json(await this.lock(() => this.checkpoint(b)));
        case "/v1/restore": return json(await this.lock(() => this.restore(b)));
        case "/v1/uncheckpoint": return json(await this.lock(() => this.uncheckpoint(b)));
        case "/v1/net": return json(await this.lock(() => this.net(b)));
        case "/v1/exec": return json(await this.lock(() => this.execVerb(b)));
        case "/v1/boot": return json(await this.lock(() => this.bootVerb(b)));
        case "/v1/logs": return json(await this.logs(b));
        case "/v1/setup-done": return json(await this.lock(() => this.setupDone()));
        case "/v1/settings": return json(await this.lock(() => this.settings(b)));
        case "/v1/forget": return json(await this.lock(() => this.forget()));
        default: return json({ error: "no such request" }, 404);
      }
    } catch (e) {
      return json({ error: L.say(e) }, (e && e.status) || 500);
    }
  }

  // A container the record says runs but the platform has stopped (a host event, the backstop): a stop, with what
  // changed since its last snapshot lost. Said in status until the next start. A stop whose container hadn't ended
  // (stopping: its disk saved first) and has now: stopped, nothing lost. Outside the lock (status, a connection) it's
  // left alone while a change runs: a stop or a restore ends the container before it records that it did.
  async reconcile(r, outside = false) {
    if (outside && this.busy) return r;
    if (r.state === "stopping" && !this.c.running) {
      const p = r.stop_pending || {};
      r.state = "stopped";
      r.stop_reason = p.why || "stop";
      r.stopped_at = Date.now();
      r.force = null;
      r.stop_pending = null;
      await this.save(r);
      await this.schedule(r);
    } else if (r.state === "running" && !this.c.running) {
      const now = Date.now();
      r.state = "stopped";
      r.stop_reason = "cloudflare";
      r.stopped_at = now;
      r.lost = { at: now, since: r.latest && r.snaps[r.latest] ? r.snaps[r.latest].made : null };
      r.force = null;
      await this.save(r);
      await this.schedule(r);
    }
    return r;
  }

  async status() {
    const r = await this.reconcile(await this.rec(), true);
    const now = Date.now();
    const cps = {};
    for (const [l, id] of Object.entries(r.checkpoints)) cps[l] = L.snapInfo(r, id);
    return {
      computer: r.computer, op: r.op, do: this.ctx.id.toString(), running: this.c.running, state: r.state,
      size: r.size, idle_min: r.idle_min, setup_done: r.setup_done,
      egress: { setting: L.egressWord(r.internet, r.allow), allow: r.allow, in_force: this.c.running ? r.force : null },
      latest: L.snapInfo(r, r.latest), checkpoints: cps, lost: r.lost, stop_reason: r.stop_reason || null,
      stop_pending: r.state === "stopping" ? r.stop_pending : null, refresh: L.refreshTrouble(r, now),
      sockets: this.sockets, last_activity: Math.max(r.last_activity || 0, this.lastActivity || 0),
      residue: L.residue(r, now), now,
    };
  }

  async waitReady(ms) {
    const t = Date.now();
    let last = "";
    while (Date.now() - t < ms) {
      try {
        const p = await this.c.exec(["true"]);
        if ((await p.exitCode) === 0) return { ok: true, ms: Date.now() - t };
        last = "not ready";
      } catch (e) {
        last = L.say(e, 120);
      }
      await scheduler.wait(100);
    }
    return { ok: false, ms: Date.now() - t, last };
  }

  async intercept(name, kind) {
    const h = kind === "allow" ? this.ctx.exports.Allow({ props: { host: name } }) : this.ctx.exports.Deny;
    await this.c.interceptOutboundHttps(name, h);
    await this.c.interceptOutboundHttp(name, h);
  }

  // Every start goes through here, each with the egress the record holds (never a default): internet on or off,
  // and with it off the sentinel and each allowed name registered before the container runs.
  async launch(r, source, refresh = false) {
    const internet = refresh ? false : r.internet;
    const plan = refresh ? [[L.SENTINEL, "deny"]] : L.startIntercepts(internet, r.allow);
    const opts = { entrypoint: ENTRY, enableInternet: internet, instance: r.size, labels: { metacomputer: "1" } };
    if (source.snapshot) {
      opts.containerSnapshot = { id: source.snapshot };
    } else {
      const img = this.c.images && this.c.images.ubuntu;
      if (!img) throw L.refuse(500, "the Worker has no image");
      opts.image = img;
    }
    const force = { internet, names: {} };
    for (const [n, k] of plan) {
      await this.intercept(n, k);
      force.names[n] = k;
    }
    this.c.start(opts);
    const ready = await this.waitReady(120000);
    if (!ready.ok) {
      try { await this.c.destroy(); } catch {}
      throw L.refuse(502, `the container didn't start (${ready.last || "no answer"})`);
    }
    try { await this.c.setInactivityTimeout(L.backstopMs(r.idle_min)); } catch {}
    if (refresh) return ready.ms;                                // the refresher's: it keeps no record
    const now = Date.now();
    if (source.snapshot && r.snaps[source.snapshot]) L.refreshDone(r.snaps[source.snapshot], now);
    r.state = "running";
    r.started_at = now;
    r.force = force;
    r.ever_started = true;
    r.lost = null;
    r.idle_failures = 0;
    r.stop_pending = null;
    this.lastActivity = now;
    await this.save(r);
    await this.schedule(r);
    return ready.ms;
  }

  async run(argv, stdin, timeoutMs, maxOut = MAX_OUT) {
    const opts = { stdout: "pipe", stderr: "pipe" };
    if (stdin) opts.stdin = new Blob([stdin]).stream();
    const proc = await this.c.exec(argv, opts);
    const out = await Promise.race([proc.output(), scheduler.wait(timeoutMs).then(() => null)]);
    if (!out) {
      try { proc.kill(9); } catch {}
      return { code: 124, stdout: new Uint8Array(0), stderr: new TextEncoder().encode("timed out") };
    }
    const so = new Uint8Array(out.stdout || 0), se = new Uint8Array(out.stderr || 0);
    return { code: out.exitCode, stdout: so.subarray(0, maxOut), stderr: se.subarray(Math.max(0, se.length - 65536)),
             truncated: so.length > maxOut };
  }

  async boot(args = []) {
    const t = Date.now();
    const o = await this.run([BOOT, ...args], null, 240000, 65536);
    const tail = new TextDecoder().decode(o.stderr.length ? o.stderr : o.stdout).slice(-300);
    if (o.code !== 0) throw L.refuse(502, `the boot script failed (exit ${o.code}): ${L.say(tail)}`);
    return Date.now() - t;
  }

  async start(b) {
    const r = await this.reconcile(await this.rec());
    if (b.idle_min !== undefined) r.idle_min = L.checkIdle(b.idle_min);
    if (this.c.running && r.state === "stopping") {
      // A stop saved the disk but Cloudflare hadn't ended the container: ended now, then started as after any stop.
      // If it still won't end, it's taken up again as it is (its disk is what that snapshot holds), unless the egress
      // asked for since needs a new container.
      try {
        await this.finishStop(r);
      } catch (e) {
        const plan = L.planNet(r.force, { internet: r.internet, allow: r.allow });
        if (!r.force || plan.restart) throw e;
        for (const [n, k] of plan.register) {
          await this.intercept(n, k);
          r.force.names[n] = k;
        }
        const now = Date.now();
        r.state = "running";
        r.started_at = now;
        r.stop_pending = null;
        r.idle_failures = 0;
        this.lastActivity = now;
        await this.save(r);
        await this.schedule(r);
        const booted = b.boot ? await this.boot() : null;
        return { already: true, resumed: true, booted_ms: booted, egress: L.egressWord(r.internet, r.allow) };
      }
    }
    if (this.c.running && r.state !== "running") {
      // A container no record names (an object that restarted in the middle of a start): ended before the start.
      try { await this.c.destroy(); } catch (e) {
        throw L.refuse(503, `a container from before is still there and Cloudflare didn't end it (${L.say(e, 120)}): try again in a minute`);
      }
    }
    if (this.c.running) {
      const booted = b.boot ? await this.boot() : null;
      await this.save(r);
      return { already: true, booted_ms: booted, egress: L.egressWord(r.internet, r.allow) };
    }
    let source;
    if (r.latest) source = { snapshot: r.latest };
    else if (b.from_image && !r.setup_done) source = { image: true };
    else throw L.refuse(409, "it has no snapshot to start from: Cloudflare stopped it before its first one");
    const ms = await this.launch(r, source);
    const booted = b.boot ? await this.boot() : null;
    return { started_ms: ms, booted_ms: booted, from: source.snapshot ? "snapshot" : "image",
             egress: L.egressWord(r.internet, r.allow) };
  }

  // A stop: the desktop's programs ended (so they save), the disk snapshotted while the container still runs, the
  // snapshot recorded (stopping), and only then the container destroyed (stopped). A snapshot that fails stops
  // nothing. A container Cloudflare doesn't end stays "stopping", said as it is: the error names the saved snapshot,
  // the alarm tries again within a minute, and a start takes it up again or ends it (finishStop).
  async stop(r, why) {
    if (!this.c.running) {
      await this.reconcile(r);
      return { already: true };
    }
    if (r.state === "stopping") return { ...(await this.finishStop(r)), snapshot: L.snapInfo(r, r.latest) };
    const t = Date.now();
    if (r.setup_done) {
      try { await this.run([BOOT, "quiesce"], null, 60000, 4096); } catch {}
    }
    const s = await this.c.snapshotContainer({ name: L.snapName(r, why, t) });
    const now = Date.now();
    L.addSnap(r, s, now);
    const prev = r.latest;
    r.latest = s.id;
    const dropped = L.dropUnused(r, prev, now);
    r.state = "stopping";
    r.stop_pending = { why, at: now, tries: 0, error: null };
    await this.save(r);
    const out = await this.finishStop(r);
    return { ...out, snapshot: L.snapInfo(r, s.id), snapshot_ms: now - t, ms: Date.now() - t, let_go: dropped };
  }

  // The end of a stop whose disk is saved: the container destroyed, then recorded as stopped. When Cloudflare doesn't
  // end it: recorded as it is (still running, its disk saved), the alarm set to try again soon, and an error that says so.
  async finishStop(r) {
    const p = r.stop_pending || { why: "stop", at: Date.now(), tries: 0, error: null };
    try {
      await this.c.destroy();
    } catch (e) {
      p.tries = (p.tries || 0) + 1;
      p.error = L.say(e, 160);
      r.stop_pending = p;
      await this.save(r);
      await this.schedule(r);
      const snap = L.snapInfo(r, r.latest);
      throw L.refuse(503, `its disk was saved (snapshot ${snap ? snap.id : "?"}), but Cloudflare didn't end the container ` +
        `(${p.error}): it still runs, and its Worker tries to end it again within a minute; nothing is lost`);
    }
    r.state = "stopped";
    r.stop_reason = p.why;
    r.stopped_at = Date.now();
    r.force = null;
    r.stop_pending = null;
    await this.save(r);
    await this.schedule(r);
    return { stopped: true };
  }

  async stopVerb() {
    return this.stop(await this.reconcile(await this.rec()), "stop");
  }

  async checkpoint(b) {
    const r = await this.reconcile(await this.rec());
    const label = L.checkLabel(b.label);
    if (!this.c.running || r.state !== "running") throw L.refuse(409, "it's stopped: a checkpoint is taken of a running computer");
    if (r.checkpoints[label] && !b.replace) throw L.refuse(409, `checkpoint ${label} exists`);
    const t = Date.now();
    const s = await this.c.snapshotContainer({ name: L.snapName(r, "ckpt", t) });
    const now = Date.now();
    L.addSnap(r, s, now);
    const old = r.checkpoints[label];
    r.checkpoints[label] = s.id;
    const dropped = L.dropUnused(r, old, now);
    await this.save(r);
    if (!this.c.running) await this.schedule(r);
    return { label, snapshot: L.snapInfo(r, s.id), ms: now - t, let_go: dropped };
  }

  // Back to a checkpoint: what runs now is discarded (no snapshot), and the container starts from the checkpoint.
  async restore(b) {
    const r = await this.reconcile(await this.rec());
    const label = L.checkLabel(b.label);
    const id = r.checkpoints[label];
    if (!id || !r.snaps[id]) throw L.refuse(404, `no checkpoint ${label}`);
    if (Date.now() - r.snaps[id].fresh >= L.EXPIRE_MS) throw L.refuse(410, `checkpoint ${label} has expired`);
    const t = Date.now();
    if (this.c.running) {
      try { await this.c.destroy(); } catch (e) {
        throw L.refuse(503, `Cloudflare didn't end the running container (${L.say(e, 120)}): nothing was changed; try again`);
      }
    }
    const prev = r.latest;
    r.latest = id;
    r.state = "stopped";
    r.force = null;
    r.stop_pending = null;
    const dropped = L.dropUnused(r, prev, Date.now());
    await this.save(r);
    const ms = await this.launch(r, { snapshot: id });
    const booted = b.boot === false ? null : await this.boot();
    return { label, started_ms: ms, booted_ms: booted, ms: Date.now() - t, let_go: dropped };
  }

  async uncheckpoint(b) {
    const r = await this.rec();
    const label = L.checkLabel(b.label);
    const id = r.checkpoints[label];
    if (!id) throw L.refuse(404, `no checkpoint ${label}`);
    delete r.checkpoints[label];
    const dropped = L.dropUnused(r, id, Date.now());
    await this.save(r);
    return { label, let_go: dropped };
  }

  async net(b) {
    const r = await this.reconcile(await this.rec());
    const want = L.wantNet(b);
    r.internet = want.internet;
    r.allow = want.allow;
    await this.save(r);                         // recorded first: whatever happens next, the next start has it
    if (!this.c.running || r.state !== "running") {
      return { setting: L.egressWord(r.internet, r.allow), in_force: null, at_next_start: true };
    }
    const plan = L.planNet(r.force, want);
    if (plan.restart) {
      await this.stop(r, "net");
      await this.launch(r, { snapshot: r.latest });
      await this.boot();
      return { setting: L.egressWord(r.internet, r.allow), in_force: r.force, restarted: true };
    }
    for (const [n, k] of plan.register) {
      await this.intercept(n, k);
      r.force.names[n] = k;
      await this.save(r);
    }
    return { setting: L.egressWord(r.internet, r.allow), in_force: r.force, restarted: false };
  }

  // A command as root, only until the computer is set up (its build's first steps); after that the key can't run
  // one: the boot script and the logs are the fixed requests left.
  async execVerb(b) {
    const r = await this.rec();
    if (r.setup_done) throw L.refuse(403, "this computer is set up: commands through the Worker are closed");
    if (!this.c.running) throw L.refuse(409, "it's stopped");
    const argv = b.argv;
    if (!Array.isArray(argv) || !argv.length || argv.length > 64 || !argv.every((a) => typeof a === "string" && a.length <= 8192)) {
      throw L.refuse(400, "argv: a list of words");
    }
    const timeout = Math.min(Math.max(Number(b.timeout_s) || 600, 1), 3600) * 1000;
    const maxOut = Math.min(Math.max(Number(b.max_output) || (1 << 20), 1024), MAX_OUT);
    const o = await this.run(argv, typeof b.stdin_b64 === "string" ? unb64(b.stdin_b64) : null, timeout, maxOut);
    return { code: o.code, stdout_b64: b64(o.stdout), stderr_b64: b64(o.stderr), truncated: !!o.truncated };
  }

  async bootVerb(b) {
    const r = await this.reconcile(await this.rec());
    if (!this.c.running || r.state !== "running") throw L.refuse(409, "it's stopped");
    return { booted_ms: await this.boot(b.restart_desktop ? ["restart-desktop"] : []) };
  }

  async logs(b) {   // (outside the lock: it reads)
    const n = Number(b.lines || 200);
    if (!Number.isInteger(n) || n < 1 || n > 5000) throw L.refuse(400, "lines: 1-5000");
    if (!this.c.running) throw L.refuse(409, "it's stopped");
    const o = await this.run(["tail", "-n", String(n), "/var/log/metacomputer-provision.log",
      "/var/log/metacomputer-cloudflare-boot.log"], null, 30000, 1 << 20);
    return { code: o.code, stdout_b64: b64(o.stdout) };
  }

  async setupDone() {
    const r = await this.rec();
    r.setup_done = true;
    await this.save(r);
    return { setup_done: true };
  }

  async settings(b) {
    const r = await this.rec();
    if (b.idle_min !== undefined) r.idle_min = L.checkIdle(b.idle_min);
    await this.save(r);
    if (this.c.running) {
      try { await this.c.setInactivityTimeout(L.backstopMs(r.idle_min)); } catch {}
    }
    await this.schedule(r);
    return { idle_min: r.idle_min };
  }

  // vm delete: a tombstone first (kept for good: every request after it is refused, 410, a captured one too, even
  // when the Worker's deletion fails and the object restarts), the container ended, the record and the alarm gone
  // (nothing is refreshed again; the nonces seen stay, under the tombstone), and what the platform keeps (its
  // snapshots, which can't be deleted) said with when each expires.
  async forget() {
    const r = await this.rec();
    const res = L.residue(r, Date.now());
    await this.ctx.storage.put("tombstone", { at: Date.now() });
    if (this.c.running) {
      try { await this.c.destroy(); } catch {}
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.delete("rec");
    return { residue: res };
  }

  async schedule(r) {
    const t = L.nextAlarm(r, this.c.running && r.state === "running", Date.now());
    if (t) await this.ctx.storage.setAlarm(t);
    else await this.ctx.storage.deleteAlarm();
  }

  async alarm() {
    await this.lock(async () => {
      const stored = await this.ctx.storage.get("rec");
      if (!stored) return;                                       // forgotten (vm delete), or the refresher
      let r = await this.reconcile(stored);
      const now = Date.now();
      if (this.c.running && r.state === "stopping") {
        try { await this.finishStop(r); } catch {}               // said by status; tried again (nextAlarm)
      } else if (this.c.running && r.state !== "running") {
        // A container no record names (the object restarted in the middle of a start): ended.
        try { await this.c.destroy(); } catch {}
      } else if (this.c.running && (r.idle_failures || 0) < L.IDLE_TRIES) {
        const d = L.idleDecision(r, this.sockets, Math.max(this.lastActivity, r.last_activity || 0), now);
        if (d.stop) {
          try {
            await this.stop(r, "idle");
          } catch (e) {
            // A snapshot that failed: tried again at the next look, IDLE_TRIES times in all. Then the container's own
            // inactivity timeout ends it, without a snapshot (a stop by Cloudflare, said in status), so it doesn't run
            // and bill unattended. (A container that didn't end after its snapshot is "stopping": tried again.)
            if (r.state !== "stopping") {
              r.idle_failures = (r.idle_failures || 0) + 1;
              r.idle_error = L.say(e, 200);
              await this.save(r);
            }
          }
        }
      }
      // The refresh, running or not: one snapshot due per look, in the refresher's container.
      r = await this.rec();
      await this.refreshDue(r, Date.now());
      await this.schedule(await this.rec());
    });
  }

  // One kept snapshot due for its refresh: restored by the refresher (its own object and container, so this computer's
  // keeps running), and recorded: renewed, or failed and tried again after its back-off (logic.js refreshFailed).
  async refreshDue(r, now) {
    const due = L.dueRefresh(r, now);
    if (!due.length || !this.env.COMPUTER) return;
    const id = due[0];
    try {
      const stub = this.env.COMPUTER.get(this.env.COMPUTER.idFromName(L.REFRESHER));
      await stub.refreshSnapshot(id, r.size);
      if (r.snaps[id]) L.refreshDone(r.snaps[id], Date.now());
    } catch (e) {
      if (r.snaps[id]) L.refreshFailed(r.snaps[id], L.say(e, 200), Date.now());
    }
    await this.save(r);
  }

  // The refresher's one request (RPC from the computer's object; the entry never reaches it): the snapshot started in
  // this object's container with the internet off and ended at once. Refused in an object that keeps a computer.
  async refreshSnapshot(id, size) {
    if (typeof id !== "string" || !SNAP_ID.test(id)) throw L.refuse(400, "not a snapshot's id");
    return this.lock(async () => {
      if (await this.ctx.storage.get("rec")) throw L.refuse(409, "the computer's own object renews no snapshot itself");
      if (!this.c) throw L.refuse(500, "no container binding");
      if (this.c.running) await this.c.destroy();                // one left from a try that was cut off
      const tmp = { size: L.SIZES.includes(size) ? size : "standard-3", idle_min: L.IDLE_MIN, internet: false, allow: [] };
      let ms;
      try {
        ms = await this.launch(tmp, { snapshot: id }, true);
      } finally {
        try { await this.c.destroy(); } catch {}
      }
      return { ms };
    });
  }

  // SSH: a websocket piped to the container's sshd (TCP from this object into the container, never public). The 101
  // carries this object's id, which the Mac checks against the computer's record before any byte goes through. An open
  // one keeps the computer from its idle stop.
  async tcp(req) {
    const r = await this.reconcile(await this.rec(), true);
    if (!this.c.running || r.state !== "running") return json({ error: "it's stopped" }, 409);
    let sock;
    try {
      sock = this.c.getTcpPort(L.PORT).connect(`127.0.0.1:${L.PORT}`, { allowHalfOpen: true });
      await sock.opened;
    } catch {
      return json({ error: "its SSH server didn't answer" }, 502);
    }
    const [client, server] = Object.values(new WebSocketPair());
    server.accept();
    try { server.binaryType = "arraybuffer"; } catch {}
    const reader = sock.readable.getReader();
    const writer = sock.writable.getWriter();
    this.sockets++;
    this.lastActivity = Date.now();
    let open = true;
    const done = async () => {
      if (!open) return;
      open = false;
      this.sockets = Math.max(0, this.sockets - 1);
      this.lastActivity = Date.now();
      try { writer.close(); } catch {}
      try { sock.close(); } catch {}
      try {
        const cur = await this.ctx.storage.get("rec");
        if (cur) { cur.last_activity = this.lastActivity; await this.ctx.storage.put("rec", cur); }
      } catch {}
    };
    server.addEventListener("message", async (ev) => {
      try {
        const d = typeof ev.data === "string" ? new TextEncoder().encode(ev.data) : new Uint8Array(ev.data);
        await writer.write(d);
      } catch {
        try { server.close(1011, "write"); } catch {}
        await done();
      }
    });
    server.addEventListener("close", () => { done(); });
    server.addEventListener("error", () => { done(); });
    (async () => {
      try {
        for (;;) {
          const { value, done: end } = await reader.read();
          if (end) break;
          server.send(value);
        }
      } catch {}
      try { server.close(1000, "eof"); } catch {}
      await done();
    })();
    return new Response(null, { status: 101, webSocket: client, headers: { "x-mc-do": this.ctx.id.toString() } });
  }
}
