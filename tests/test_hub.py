#!/usr/bin/env python3
"""Offline checks for the hub (lib/mcp_hub.py), its clients (lib/mcomp_sdk.py,
lib/mcp_lazy.py, `metacomputer call`) and the coordinate guard. No VM needed: a
stand-in driver (FAKE_DRIVER below) speaks MCP over stdio, keeps element tokens,
driver sessions and recordings for as long as it runs, and logs every message it
gets. Everything lives in a temporary folder; events go to a temporary events folder.

    python3 tests/test_hub.py
"""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD = tempfile.mkdtemp(prefix="mcomp-hub-")
RUN = os.path.join(TD, "run")
STATE = os.path.join(TD, "driver")
os.makedirs(STATE)
# Before importing owners/mcp_hub: they read where the owners' claims live from the environment.
os.environ.update(MCOMP_RUN_DIR=RUN, MCOMP_LOG_DIR=os.path.join(TD, "logs"), MCOMP_EVENTS_DIR=os.path.join(TD, "events"),
                  MCOMP_EVENTS_SPOOL=os.path.join(TD, "spool"), MCOMP_VM_NAME="hubtest", MCOMP_SSH_PORT="59995",
                  MCOMP_ACTION_LOG="off", MCOMP_DRIVER_MODE="standard", MCOMP_COORD_GUARD="on", MCOMP_MCP_HUB="on",
                  MCOMP_ACTIVE_S="600")
for k in ("MCOMP_CONFIG", "MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
import mcp_hub  # noqa: E402
import mcomp_sdk  # noqa: E402
import owners  # noqa: E402
import termsafe  # noqa: E402

PY = sys.executable
FAKE = os.path.join(TD, "fake_driver.py")
results = []

# A stand-in for Cua Driver's MCP server. Windows come from STATE/windows.json (the test moves them);
# snapshots, sessions and recordings live as long as the process; every message is logged.
FAKE_DRIVER = r'''
import json, os, sys, time
STATE = sys.argv[1]
TOOLS = json.load(open(os.path.join(STATE, "tools.json")))
PNG = "iVBORw0KGgo="
snaps, sessions, recording = {}, set(), {}
counter = [0]
out = sys.stdout

def log(m):
    with open(os.path.join(STATE, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"pid": os.getpid(), "msg": m}) + "\n")

def windows():
    return json.load(open(os.path.join(STATE, "windows.json")))

def ranked(ws):
    return [dict(w, z_index=i) for i, w in enumerate(ws)]

def send(m):
    out.write(json.dumps(m) + "\n"); out.flush()

def ok(mid, text, sc=None, images=0, err=False):
    r = {"content": [{"type": "text", "text": text}] + [{"type": "image", "data": PNG, "mimeType": "image/png"}] * images}
    if sc is not None:
        r["structuredContent"] = sc
    if err:
        r["isError"] = True
    send({"jsonrpc": "2.0", "id": mid, "result": r})

def find(wid):
    return next((w for w in windows() if w["window_id"] == wid), None)

for line in sys.stdin:
    m = json.loads(line)
    log(m)
    mid, method = m.get("id"), m.get("method")
    if mid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
              "serverInfo": {"name": "fake-driver", "version": "0"}, "instructions": "fake"}})
        continue
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        continue
    if method != "tools/call":
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no such method"}})
        continue
    name, a = m["params"]["name"], m["params"].get("arguments") or {}
    sess = a.get("session") or ""
    if name == "list_windows":
        ws = [w for w in windows() if (not a.get("pid") or w["pid"] == a["pid"])
              and (not a.get("app") or a["app"].lower() in w["app_name"].lower())]
        ok(mid, f"{len(ws)} windows", {"windows": ranked(ws)})
    elif name == "get_window_state":
        w = find(a.get("window_id")) or {}
        counter[0] += 1
        sid = f"s{counter[0]:08d}"
        snaps[sid] = (w.get("window_id"), sess)
        shot = a.get("include_screenshot", True) is not False
        b = w.get("bounds") or {}
        sc = {"pid": w.get("pid"), "app_name": w.get("app_name"), "window_id": w.get("window_id"), "snapshot_id": sid,
              "window_bounds": b}
        if shot:
            sc.update(screenshot_width=b.get("width"), screenshot_height=b.get("height"), screenshot_frame_valid=True)
        ok(mid, f"snapshot_id={sid} session={sess}", sc, images=1 if shot else 0)
    elif name == "get_desktop_state":
        ok(mid, "desktop", {"windows": ranked(windows()), "screen_width": 1920, "screen_height": 1080,
                            "screenshot_width": 1920, "screenshot_height": 1080}, images=1)
    elif name in ("click", "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll", "drag",
                  "move_cursor", "set_value", "launch_app", "mouse_button_down"):
        tok = a.get("element_token")
        if tok and tok.split(":")[0] not in snaps:
            ok(mid, f"unknown element_token {tok}", {"code": "stale_token"}, err=True)
        else:
            ok(mid, f"{name} done (session={sess}, mode={a.get('delivery_mode')})", {"effect": "confirmed", "route": "accessibility"})
    elif name == "run_steps":
        n = len(a.get("steps") or [])
        sc = {"executed": n, "ok": True}
        imgs = 0
        obs = a.get("observe")
        if isinstance(obs, dict) and obs.get("include_screenshot"):
            w = find(obs.get("window_id") or a.get("window_id")) or {}
            b = w.get("bounds") or {}
            sc["observation"] = {"ok": True, "state": {"window_id": w.get("window_id"), "window_bounds": b,
                                 "screenshot_width": b.get("width"), "screenshot_height": b.get("height"),
                                 "screenshot_frame_valid": True}}
            imgs = 1
        ok(mid, f"run_steps: {n}/{n} steps ok", sc, images=imgs)
    elif name == "start_recording":
        recording[sess] = True
        ok(mid, "recording", {"recording": True})
    elif name == "stop_recording":
        recording.pop(sess, None)
        ok(mid, "stopped", {"recording": False})
    elif name == "get_recording_state":
        ok(mid, "state", {"recording": bool(recording.get(sess)), "video_active": bool(recording.get(sess))})
    elif name == "end_session":
        recording.pop(sess, None)
        ok(mid, f"ended {sess}", {"active": False})
    elif name == "whoami":
        ok(mid, json.dumps({"driver_pid": os.getpid(), "session": sess, "tag": a.get("tag")}),
           {"driver_pid": os.getpid(), "session": sess, "tag": a.get("tag"), "id_seen": mid})
    elif name == "echo":
        ok(mid, str(a.get("tag")), {"tag": a.get("tag")})
    elif name == "slow":
        time.sleep(float(a.get("s", 1)))
        ok(mid, "slow done")
    elif name == "big":
        ok(mid, "x" * int(a.get("size", 1000)))
    elif name == "huge":
        out.write('{"jsonrpc":"2.0","id":' + json.dumps(mid) + ',"result":{"content":[{"type":"text","text":"'
                  + "y" * int(a.get("size", 1000)) + '"}]}}\n'); out.flush()
    elif name == "nonl":
        out.write('{"jsonrpc":"2.0","id":' + json.dumps(mid) + ',"pad":"'); out.flush()
        while True:
            out.write("z" * 65536); out.flush()
    elif name == "die":
        sys.exit(0)
    elif name == "deaf":
        time.sleep(3600)                 # stops reading: what the hub writes next fills the pipe
    elif name == "expire":
        ok(mid, "the capability manifest expired for this session", err=True)
    elif name == "listchange":
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        ok(mid, "changed")
    else:
        ok(mid, f"{name} ok (session={sess})")
'''


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)


# --- the stand-in driver's world ------------------------------------------------------------
W = 11          # the target window (pid 50)
CHROME = 21     # a Chrome window (pid 77)


def win(wid, pid, app, x, y, w, h, title=None):
    return {"window_id": wid, "pid": pid, "app_name": app, "title": title or app,
            "bounds": {"x": x, "y": y, "width": w, "height": h}, "is_on_screen": True}


BASE_WINDOWS = [win(1, 10, "Xfdesktop", 0, 0, 1920, 1080), win(W, 50, "Mousepad", 100, 100, 800, 600),
                win(CHROME, 77, "Google-chrome", 1000, 100, 800, 700), win(2, 11, "Xfce4-panel", 0, 0, 1920, 27)]


def set_windows(ws):
    p = os.path.join(STATE, "windows.json")
    json.dump(ws, open(p + ".tmp", "w"))
    os.replace(p + ".tmp", p)


def calls():
    try:
        return [json.loads(x) for x in open(os.path.join(STATE, "calls.jsonl"))]
    except OSError:
        return []


def tool_calls(name=None, since=0):
    out = []
    for c in calls()[since:]:
        m = c["msg"]
        if m.get("method") == "tools/call" and (name is None or m["params"]["name"] == name):
            out.append(c)
    return out


def setup_driver():
    seed = json.load(open(os.path.join(LAB, "config", "driver-mcp-cache.json")))
    tools = list(seed["tools"]["tools"])
    extra = {"type": "object", "properties": {"session": {"type": "string"}, "s": {"type": "number"},
                                              "size": {"type": "integer"}, "tag": {"type": "string"}}}
    for n in ("whoami", "echo", "slow", "big", "huge", "nonl", "die", "deaf", "expire", "listchange"):
        tools.append({"name": n, "description": "test tool", "inputSchema": extra})
    json.dump(tools, open(os.path.join(STATE, "tools.json"), "w"))
    open(FAKE, "w").write(FAKE_DRIVER)
    set_windows(BASE_WINDOWS)


def env(**extra):
    e = dict(os.environ)
    e["MCOMP_HUB_DRIVER_CMD"] = json.dumps([PY, FAKE, STATE])
    e.update({k: str(v) for k, v in extra.items()})
    return e


class Running:
    """A hub served from a thread of this process (so its internals can be inspected)."""

    def __init__(self, idle_s=600, **extra):
        self.env = env(**extra)
        self.hub = mcp_hub.Hub(env=self.env)
        self.server = mcp_hub.Server(self.hub, idle_s)
        self.rc = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        sock = mcp_hub.paths(self.env)["sock"]
        end = time.time() + 5
        while time.time() < end and not (os.path.exists(sock) and self.hub.log):
            time.sleep(0.02)
        time.sleep(0.05)

    def _run(self):
        self.rc = self.server.run()

    def stop(self):
        self.server.stop("test")
        self.t.join(10)


def client(r, kind="sdk", session=None):
    return mcomp_sdk.Hub(env=r.env, kind=kind, session=session, timeout=20).connect()


def foreign_claim():
    """Another Claude Code session acting on the computer: a live process with a claim."""
    other = subprocess.Popen(["sleep", "120"])
    now = time.time()
    os.makedirs(owners.OWNERS_DIR, exist_ok=True)
    json.dump({"bridge_pid": other.pid, "claude_pid": 999999, "cwd": "/another/project", "started": now,
               "last_call": now, "last_tool": "click"}, open(owners.claim_path(other.pid), "w"))
    return other


def drop_foreign(other):
    owners.drop_claim(other.pid)
    other.kill()
    other.wait()


def clear_claims():
    if os.path.isdir(owners.OWNERS_DIR):
        for f in os.listdir(owners.OWNERS_DIR):
            if f.endswith(".json"):
                try:
                    os.remove(os.path.join(owners.OWNERS_DIR, f))
                except FileNotFoundError:
                    pass                             # a client's own detach got there first


def local_events():
    p = os.path.join(TD, "logs", "events.jsonl")
    try:
        return [json.loads(x) for x in open(p)]
    except OSError:
        return []


# --- checks ------------------------------------------------------------------------------
def socket_checks():
    print("the socket, the lock, the uid check")
    r = Running()
    p = mcp_hub.paths(r.env)
    st_dir, st_sock = os.stat(p["sockdir"]), os.stat(p["sock"])
    check("the socket is 0600 in a 0700 folder of this user's", stat.S_IMODE(st_dir.st_mode) == 0o700
          and stat.S_IMODE(st_sock.st_mode) == 0o600 and st_sock.st_uid == os.getuid(),
          (oct(st_dir.st_mode), oct(st_sock.st_mode)))
    a, b = socket.socketpair()
    uid, pid = mcp_hub.peer_cred(a)
    check("the peer's uid and pid come from the kernel (LOCAL_PEERCRED / LOCAL_PEERPID)",
          uid == os.getuid() and pid == os.getpid(), (uid, pid))
    a.close()
    b.close()
    proc = subprocess.run([PY, os.path.join(LAB, "lib", "mcp_hub.py"), "serve"], env=r.env, capture_output=True,
                          text=True, timeout=20)
    check("a second hub for the same VM is refused (exit 3; the first keeps the lock)", proc.returncode == 3
          and "already running" in proc.stderr and mcp_hub.hub_pid(r.env) == os.getpid(), (proc.returncode, proc.stderr))
    r.server.expected_uid = os.getuid() + 1          # as if every peer were another user
    try:
        client(r)
        refused = False
    except mcp_hub.HubError:
        refused = True
    log = open(mcp_hub.paths(r.env)["log"]).read()
    check("a peer whose uid isn't the hub's own is refused before a word is read", refused and "refused a connection from uid" in log)
    r.server.expected_uid = os.getuid()
    long_env = dict(r.env, MCOMP_RUN_DIR="/" + "x" * 120)
    lp = mcp_hub.paths(long_env)["sock"]
    check("a RUN_DIR too long for a Unix socket gets a short private folder in /tmp", lp.startswith(f"/tmp/mcomp-hub-{os.getuid()}-")
          and len(lp) < 100, lp)
    r.stop()
    check("stopping removes the socket and frees the lock", not os.path.exists(p["sock"]) and mcp_hub.hub_pid(r.env) is None)


def multiplex_checks():
    print("multiplexing")
    r = Running()
    a, b = client(r, "sdk"), client(r, "sdk")
    n0 = len(calls())
    box = {}

    def go(h, tag):
        box[tag] = h.call("whoami", {"tag": tag}).structured
    ts = [threading.Thread(target=go, args=(h, t)) for h, t in ((a, "A"), (b, "B"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("two clients with the same request ids each get their own answer", box["A"]["tag"] == "A"
          and box["B"]["tag"] == "B", box)
    check("they share one driver connection (one driver process)", box["A"]["driver_pid"] == box["B"]["driver_pid"], box)
    seen = [c["msg"]["id"] for c in tool_calls("whoami", n0)]
    check("the driver sees distinct ids, rewritten per client", len(set(seen)) == 2 and all(str(x).startswith("c") for x in seen), seen)
    a.tools()
    b.request("initialize", {"protocolVersion": "2025-06-18"})
    inits = [c for c in calls() if c["msg"].get("method") == "initialize"]
    lists = [c for c in calls() if c["msg"].get("method") == "tools/list"]
    check("initialize and tools/list are answered by the hub (the driver saw one of each, at connect)",
          len(inits) == 1 and len(lists) == 1, (len(inits), len(lists)))
    got = []
    raw = mcomp_sdk.open_connection(r.env, kind="sdk")
    raw.on_message = got.append
    raw.start()
    a.call("listchange", {})
    time.sleep(0.5)
    check("the driver's tools/list_changed reaches the clients", any(m.get("method") == "notifications/tools/list_changed" for m in got), got)
    raw.close()
    st = a.status()
    check("status: pid, clients, the driver connection", st["pid"] == os.getpid() and len(st["clients"]) == 2
          and st["driver"]["connected"], st)
    q = mcp_hub.query_status(r.env)
    check("`hub status` reads the same from outside", q and q["pid"] == os.getpid() and len(q["clients"]) == 2, q)
    a.close()
    b.close()
    r.stop()


def session_checks():
    print("driver sessions: per client, and `call --session`")
    r = Running()
    n0 = len(calls())
    cc = mcomp_sdk.open_connection(r.env, kind="claude-code")
    replies = []
    cc.on_message = replies.append
    cc.start()
    cc.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}})
    cc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {"session": "mine"}}})
    end = time.time() + 5
    while len(replies) < 2 and time.time() < end:
        time.sleep(0.05)
    sess = [json.loads(m["result"]["content"][0]["text"])["session"] for m in sorted(replies, key=lambda m: m["id"])]
    check("a Claude Code session gets a driver session of its own; its own label is kept",
          len(sess) == 2 and sess[0].startswith("mcp-") and sess[1] == "mine", sess)
    label = sess[0] if sess else ""
    cc.close()
    time.sleep(0.5)
    ended = [c for c in tool_calls("end_session", n0) if c["msg"]["params"]["arguments"].get("session") == label]
    check("its driver session ends when it disconnects (as its own connection used to)", len(ended) == 1, label)

    def cli(*args, extra=None):
        return subprocess.run([PY, os.path.join(LAB, "lib", "mcomp_sdk.py"), "call", *args], env=dict(r.env, **(extra or {})),
                              capture_output=True, text=True, timeout=60)
    one = cli("--session", "s1", "get_window_state", json.dumps({"pid": 50, "window_id": W}))
    sid = next((w.split("=")[1] for w in one.stdout.split() if w.startswith("snapshot_id=")), "")
    two = cli("--session", "s1", "click", json.dumps({"element_token": f"{sid}:1"}))
    check("`call --session`: an element token from one call works in the next", one.returncode == 0 and sid
          and two.returncode == 0 and "session=s1" in two.stdout, (one.stdout[-200:], two.stdout[-200:], two.stderr[-200:]))
    off = dict(r.env, MCOMP_MCP_HUB="off")
    one = cli("get_window_state", json.dumps({"pid": 50, "window_id": W}), extra={"MCOMP_MCP_HUB": "off"})
    sid2 = next((w.split("=")[1] for w in one.stdout.split() if w.startswith("snapshot_id=")), "")
    two = subprocess.run([PY, os.path.join(LAB, "lib", "mcomp_sdk.py"), "call", "click", json.dumps({"element_token": f"{sid2}:1"})],
                         env=off, capture_output=True, text=True, timeout=60)
    check("(without the hub, MCP_HUB=off, each call has its own driver and the token is unknown)",
          one.returncode == 0 and two.returncode == 1 and "unknown element_token" in two.stdout, (two.stdout, two.stderr))
    rec = cli("--session", "rec1", "start_recording", "{}")
    st = mcp_hub.query_status(r.env)
    check("a recording started by a one-shot call is still running after it (the hub keeps the connection)",
          rec.returncode == 0 and st["recordings"] == 1, st)
    cli("--session", "rec1", "stop_recording", "{}")
    r.stop()


def lifecycle_checks():
    print("idle exit, recordings, settings, the driver dropping")
    r = Running(idle_s=1)
    h = client(r)
    h.call("whoami", {})
    h.close()
    r.t.join(5)
    check("the hub exits after HUB_IDLE with no client", not r.t.is_alive() and r.server.why.startswith("idle"), r.server.why)

    r = Running(idle_s=1)
    h = client(r, session="rec")
    h.call("start_recording", {})
    h.close()
    time.sleep(2.5)
    alive = r.t.is_alive()
    h = client(r, session="rec")
    h.call("stop_recording", {})
    h.close()
    r.t.join(5)
    check("…but not while a driver recording runs; it exits once the recording stops", alive and not r.t.is_alive())

    r = Running()
    keep = client(r)
    try:
        client_other = mcomp_sdk.Hub(env=dict(r.env, MCOMP_COORD_GUARD="off"), timeout=20).connect()
        differ = None
        client_other.close()
    except mcp_hub.HubError as e:
        differ = str(e)
    check("a client with other settings is refused while another client uses the hub", differ and "different settings"
          in differ and "coord_guard" in differ, differ)
    keep.close()
    time.sleep(0.3)
    h = mcomp_sdk.Hub(env=dict(r.env, MCOMP_COORD_GUARD="off"), timeout=20).connect()
    r.t.join(5)
    ok = h.call("whoami", {}).structured
    newpid = mcp_hub.hub_pid(r.env)
    check("…and when it's alone the hub restarts with its settings", not r.t.is_alive() and newpid and newpid != os.getpid()
          and ok.get("driver_pid"), (newpid, ok))
    h.close()
    mcp_hub.stop_hub(r.env)

    r = Running()
    a, b = client(r), client(r)
    first = a.call("whoami", {}).structured["driver_pid"]
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", a.call("slow", {"s": 5})))
    t.start()
    time.sleep(0.5)
    os.kill(first, signal.SIGKILL)                  # the driver's connection drops mid-call
    t.join(10)
    slow = box.get("r")
    check("the driver dropping fails every pending request clearly", slow is not None and slow.is_error
          and "closed during this call" in slow.text, slow.text if slow else None)
    again = b.call("whoami", {}).structured
    check("the next call reconnects (a new driver process)", again.get("driver_pid") and again["driver_pid"] != first, again)
    a.close()
    b.close()
    r.stop()


def limit_checks():
    print("message limits and a client that stops reading")
    old = (mcp_hub.MAX_CLIENT_MSG, mcp_hub.MAX_UPSTREAM_MSG, mcp_hub.MAX_BACKLOG_MSGS, mcp_hub.MAX_BACKLOG_BYTES,
           mcp_hub.WRITE_TIMEOUT_S)
    mcp_hub.MAX_CLIENT_MSG, mcp_hub.MAX_UPSTREAM_MSG = 100_000, 300_000
    mcp_hub.MAX_BACKLOG_MSGS, mcp_hub.MAX_BACKLOG_BYTES = 16, 1_500_000
    try:
        r = Running()
        good = client(r)
        s = mcp_hub.connect_or_start(r.env, start=False)
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 0, "method": "metacomputer/hello",
                               "params": {"protocol": 1, "kind": "sdk"}}) + "\n").encode())
        try:
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{"pad":"'
                      + b"p" * 120_000 + b'"}}}\n')
        except OSError:
            pass                                     # the hub stops reading at the limit and closes
        s.settimeout(5)
        got = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
        except OSError:
            pass
        s.close()
        check("a client message over the limit gets an error and the connection closes", b"message too large" in got, got[-200:])
        check("…the others carry on", good.call("whoami", {}).structured.get("driver_pid"))
        r1 = good.call("huge", {"size": 400_000})
        check("a driver message over the limit fails its request and drops the driver connection",
              r1.is_error and "larger than the hub accepts" in r1.text, r1.text[:200])
        check("…and the next call reconnects", good.call("whoami", {}).structured.get("driver_pid"))
        r2 = good.call("nonl", {}, timeout=20)
        check("a driver line that never ends is cut off at the limit (memory stays bounded)",
              r2.is_error and "larger than the hub accepts" in r2.text, r2.text[:200])
        mcp_hub.WRITE_TIMEOUT_S = 2
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("deaf", good.call("deaf", {}, timeout=30)))
        th.start()
        time.sleep(0.5)
        t0 = time.time()
        sc = client(r)
        stuck = sc.call("whoami", {"tag": "w" * 90_000}, timeout=30)
        took = time.time() - t0
        sc.close()
        th.join(10)
        check("a driver that stops reading is dropped after the write deadline; its calls fail clearly",
              stuck.is_error and took < 6 and "closed during this call" in (box.get("deaf").text if box.get("deaf") else ""),
              (took, stuck.text[:120]))
        check("…and the next call reconnects", good.call("whoami", {}).structured.get("driver_pid"))

        # a client that sends requests and never reads
        hung = mcp_hub.connect_or_start(r.env, start=False)
        hung.sendall((json.dumps({"jsonrpc": "2.0", "id": 0, "method": "metacomputer/hello",
                                  "params": {"protocol": 1, "kind": "sdk"}}) + "\n").encode())
        for i in range(60):
            hung.sendall((json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                                      "params": {"name": "big", "arguments": {"size": 150_000}}}) + "\n").encode())
        worst = 0.0
        for _ in range(10):
            t0 = time.time()
            good.call("whoami", {})
            worst = max(worst, time.time() - t0)
        time.sleep(1.0)
        n_clients = len(r.hub.active_clients())
        log = open(mcp_hub.paths(r.env)["log"]).read()
        check("a client that stops reading doesn't hold up the others (each call under 2 s)", worst < 2.0, worst)
        check("…and once its backlog is full it's disconnected", "isn't reading: disconnected" in log and n_clients == 1,
              (n_clients, log[-300:]))
        hung.close()
        good.close()
        r.stop()
    finally:
        (mcp_hub.MAX_CLIENT_MSG, mcp_hub.MAX_UPSTREAM_MSG, mcp_hub.MAX_BACKLOG_MSGS, mcp_hub.MAX_BACKLOG_BYTES,
         mcp_hub.WRITE_TIMEOUT_S) = old


def owner_checks():
    print("one controller at a time, for every kind of client")
    clear_claims()
    r = Running()
    other = foreign_claim()
    try:
        for kind in ("claude-code", "call", "sdk"):
            h = client(r, kind)
            n0 = len(tool_calls("press_key"))
            res = h.call("press_key", {"pid": 50, "window_id": W, "key": "a"})
            look = h.call("list_windows", {})
            check(f"{kind}: an acting call is refused while another session acts; nothing reaches the driver",
                  res.is_error and "in use by another controller" in res.text and "/another/project" in res.text
                  and len(tool_calls("press_key")) == n0, res.text[:200])
            check(f"{kind}: read-only calls still work", not look.is_error)
            h.close()
        evs = [e for e in local_events() if e.get("type") == "in_use"]
        check("the refusal tells the user (needs_you.in_use, with the project)", evs and evs[-1]["data"]["others"][0]["project"]
              == "/another/project", evs[-1:] if evs else None)
    finally:
        drop_foreign(other)
    h = client(r, "call")
    res = h.call("press_key", {"pid": 50, "window_id": W, "key": "a"})
    holder = r.hub.active_clients()[0].holder
    claim = json.load(open(owners.claim_path(holder))) if os.path.exists(owners.claim_path(holder)) else {}
    check("with the computer free, a call acts and claims it for its holder (Claude Code, or the shell)",
          not res.is_error and claim.get("last_tool") == "press_key", (res.text[:100], holder, claim))
    h.close()
    clear_claims()

    # the check and the claim are one step under the owners lock
    h = client(r, "sdk")
    flag = threading.Event()

    def holder_thread():
        with owners.lock():
            flag.set()
            time.sleep(1.2)
    t = threading.Thread(target=holder_thread)
    t.start()
    flag.wait(2)
    t0 = time.time()
    res = h.call("press_key", {"pid": 50, "window_id": W, "key": "b"})
    waited = time.time() - t0
    t.join()
    t0 = time.time()
    with owners.lock():
        th = threading.Thread(target=lambda: h.call("list_windows", {}))
        th.start()
        th.join(3)
    read_wait = time.time() - t0
    check("an acting call waits for the owners lock (check and claim in one step)", not res.is_error and waited > 1.0, waited)
    check("a read-only call doesn't take it", read_wait < 1.0, read_wait)
    h.close()
    clear_claims()
    r.stop()


def chrome_checks():
    print("the Chrome guard, for every kind of client")
    r = Running()
    r.hub.input_reset = lambda: None                 # the real one runs `metacomputer input-reset` in the VM
    for kind in ("claude-code", "call", "sdk"):
        h = client(r, kind)
        n0 = len(calls())
        sv = h.call("set_value", {"pid": 77, "window_id": CHROME, "element_token": "s1:1", "value": "secret-typed"})
        rs = h.call("run_script", {"script": "const a = await cua.getApp({app: 'Google Chrome'}); return 1;"})
        ck = h.call("click", {"pid": 77, "window_id": CHROME, "element_token": "x"})
        sent = [c["msg"]["params"] for c in tool_calls(None, n0)]
        names = [p["name"] for p in sent]
        check(f"{kind}: set_value and run_script aimed at Chrome are refused before the driver",
              sv.is_error and "browser_type" in sv.text and rs.is_error and "run_steps" in rs.text
              and "set_value" not in names and "run_script" not in names, (sv.text[:80], rs.text[:80], names))
        fg = [p for p in sent if p["name"] == "click"]
        check(f"{kind}: input aimed at Chrome goes in the foreground", fg and fg[-1]["arguments"].get("delivery_mode") == "foreground", fg)
        h.close()
    clear_claims()
    r.stop()


def bounded_checks():
    print("bounded mode")
    r = Running(MCOMP_DRIVER_MODE="bounded")
    h = client(r, "call")
    n0 = len(calls())
    res = h.call("launch_app", {"name": "mousepad"})
    sent = [c["msg"]["params"]["arguments"] for c in tool_calls("launch_app", n0)]
    check("launch_app by name is sent by its absolute path", sent and sent[-1].get("launch_path") == "/usr/bin/mousepad"
          and "bounded mode" in res.text, (sent, res.text[:120]))
    first = h.call("whoami", {}).structured["driver_pid"]
    e = h.call("expire", {})
    after = h.call("whoami", {})
    check("when the bounded session ends, its clients are told to reconnect, and stay ended",
          "bounded session has ended" in e.text and after.is_error and "bounded session has ended" in after.text, after.text[:120])
    h2 = client(r, "call")
    fresh = h2.call("whoami", {}).structured
    check("a client that connects afterwards gets a new driver connection", fresh.get("driver_pid") and fresh["driver_pid"] != first, fresh)
    h.close()
    h2.close()
    clear_claims()
    r.stop()


def actionlog_checks():
    print("the action log through the hub")
    adir = os.path.join(TD, "actions")
    key = os.path.join(TD, "keys", "alog")
    extra = dict(MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=adir, MCOMP_ACTION_LOG_KEY=key)
    r = Running(**extra)
    r.hub.spawn = lambda: subprocess.Popen([PY, os.path.join(LAB, "lib", "mcp_relay.py"), PY, FAKE, STATE],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=dict(r.env, MCOMP_ACTION_LOG="on"))
    r.hub.input_reset = lambda: None
    n0 = len(calls())
    a, b = client(r, "call", session="logged"), client(r, "sdk")
    a.call("press_key", {"pid": 50, "window_id": W, "key": "a"})
    b.call("list_windows", {})
    b.call("set_value", {"pid": 77, "window_id": CHROME, "element_token": "s1:1", "value": "pw"})
    a.close()
    b.close()
    r.stop()
    time.sleep(0.5)
    import actionlog
    files = actionlog.files(adir)
    recs = [json.loads(x) for x in open(files[-1])] if files else []
    callrecs = [x for x in recs if x.get("type") == "call"]
    kinds = {x["tool"]: (x.get("client") or {}).get("kind") for x in callrecs}
    check("one log for the hub's connection; each call names the client that made it",
          len(files) == 1 and recs[0]["type"] == "start" and kinds.get("press_key") == "call"
          and kinds.get("list_windows") == "sdk", (len(files), kinds))
    ref = [x for x in recs if x.get("type") == "refused"]
    check("the hub's refusals are logged with the client", ref and ref[0]["tool"] == "set_value"
          and (ref[0].get("client") or {}).get("kind") == "sdk" and "pw" not in json.dumps(ref), ref[:1])
    leaked = [c for c in calls()[n0:] if str(c["msg"].get("method", "")).startswith("notifications/metacomputer/")]
    check("the hub's notes never reach the driver", not leaked, leaked[:1])
    v = actionlog.verify_file(files[-1], adir, key + ".pub") if files else {}
    check("the log verifies, with a final signature", v.get("ok") and v.get("final"), v)
    clear_claims()


def coord_checks():
    print("the coordinate guard")
    clear_claims()
    set_windows(BASE_WINDOWS)
    r = Running()
    h = client(r, "call", session="cg")
    n_lw = lambda: len(tool_calls("list_windows"))  # noqa: E731

    def shot():
        h.call("get_window_state", {"pid": 50, "window_id": W})

    def click(x=400, y=300, **kw):
        return h.call("click", dict({"pid": 50, "window_id": W, "x": x, "y": y}, **kw))
    shot()
    res = click()
    check("allowed: the windows haven't changed since the screenshot", not res.is_error, res.text[:160])
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(width=500, height=400)
    set_windows(ws)
    n0 = len(tool_calls("click"))
    res = click()
    sc = res.structured
    check("refused: the window was resized since the screenshot (code, hint, no input sent)",
          res.is_error and sc.get("code") == "windows_changed_since_screenshot" and "800x600" in res.text
          and "500x400" in res.text and "take a fresh one" in sc.get("hint", "") and len(tool_calls("click")) == n0,
          (res.text[:240], sc))
    check("…the refusal says what to call next", (sc.get("next_call") or {}).get("tool") == "get_window_state"
          and sc["next_call"]["arguments"].get("session") == "cg", sc.get("next_call"))
    shot()
    check("allowed again after a fresh screenshot", not click().is_error)
    ws = json.loads(json.dumps(BASE_WINDOWS))
    set_windows(ws)
    shot()
    ws.append(win(31, 90, "Gnome-calculator", 300, 250, 400, 500, "Calculator"))
    set_windows(ws)
    res = click(400, 300)
    check("refused: a window now covers the point (named by app and id)", res.is_error and "Gnome-calculator (window 31) now covers"
          in res.text, res.text[:200])
    check("allowed: a point that window doesn't cover", not click(50, 50).is_error)
    set_windows(BASE_WINDOWS)
    shot()
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(x=200, y=150)
    set_windows(ws)
    check("allowed: the window only moved (window-local pixels follow it)", not click().is_error)
    shot()
    ws[1]["bounds"].update(width=600)
    set_windows(ws)
    res = h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"click": {"role": "button", "name": "OK"}},
                                                                    {"tool": "click", "args": {"x": 10, "y": 10}}]})
    check("refused: a run_steps batch with a pixel step, before any step runs", res.is_error
          and res.structured.get("failed_step") == 1 and "step 2" in res.text, res.text[:200])
    before = n_lw()
    res = h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"click": {"role": "button", "name": "OK"}},
                                                                    {"type_text": {"text": "hi"}}]})
    res2 = h.call("press_key", {"pid": 50, "window_id": W, "key": "Return"})
    check("non-pixel actions pass with no extra list_windows (no added latency)", not res.is_error and not res2.is_error
          and n_lw() == before, (n_lw(), before))
    set_windows(BASE_WINDOWS)
    shot()
    before = n_lw()
    res = h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"click": {"x": 10, "y": 10}},
                                                                    {"click": {"x": 20, "y": 20}},
                                                                    {"double_click": {"x": 30, "y": 30}}]})
    check("one list_windows for a whole batch of pixel steps", not res.is_error and n_lw() - before == 1, n_lw() - before)
    shot()
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(height=650)
    set_windows(ws)
    res = h.call("drag", {"pid": 50, "window_id": W, "from_x": 10, "from_y": 10, "to_x": 200, "to_y": 200})
    res2 = h.call("scroll", {"pid": 50, "window_id": W, "x": 10, "y": 10, "direction": "down"})
    res3 = h.call("type_text", {"pid": 50, "window_id": W, "x": 10, "y": 10, "text": "x"})
    check("drag, scroll and type_text with x,y are checked too", res.is_error and res2.is_error and res3.is_error)
    res4 = h.call("move_cursor", {"x": 10, "y": 10})
    check("move_cursor without a target (the overlay only) isn't input: allowed", not res4.is_error, res4.text[:120])
    # desktop pixels
    set_windows(BASE_WINDOWS)
    h.call("get_desktop_state", {})
    check("desktop pixels: allowed while nothing moved", not h.call("click", {"scope": "desktop", "x": 300, "y": 300}).is_error)
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(x=600)
    set_windows(ws)
    res = h.call("click", {"scope": "desktop", "x": 300, "y": 300})
    check("desktop pixels: refused when the top window at the point changed", res.is_error and "top window" in res.text, res.text[:200])
    h.call("get_desktop_state", {})
    check("desktop pixels: allowed after a fresh get_desktop_state", not h.call("click", {"scope": "desktop", "x": 300, "y": 300}).is_error)
    # a run_steps observe with a screenshot counts as one
    set_windows(BASE_WINDOWS)
    h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"press_key": {"key": "End"}}],
                         "observe": {"include_screenshot": True}})
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(width=700)
    set_windows(ws)
    check("a screenshot inside a run_steps result is a screenshot too", click().is_error)
    other = client(r, "call", session="fresh")
    check("no screenshot the hub saw (another session): the driver's own checks decide", not other.call(
        "click", {"pid": 50, "window_id": W, "x": 400, "y": 300}).is_error)
    other.close()
    h.close()
    r.stop()
    set_windows(BASE_WINDOWS)
    r = Running(MCOMP_COORD_GUARD="off")
    h = client(r, "call", session="cg")
    n0 = len(tool_calls("list_windows"))
    h.call("get_window_state", {"pid": 50, "window_id": W})
    ws = json.loads(json.dumps(BASE_WINDOWS))
    ws[1]["bounds"].update(width=500)
    set_windows(ws)
    res = h.call("click", {"pid": 50, "window_id": W, "x": 400, "y": 300})
    check("COORD_GUARD=off: allowed, and no list_windows asked for", not res.is_error and len(tool_calls("list_windows")) == n0)
    h.close()
    r.stop()
    set_windows(BASE_WINDOWS)
    clear_claims()


def front_checks():
    print("mcp-lazy, the Claude Code side")
    clear_claims()
    r = Running()
    p = subprocess.Popen([PY, os.path.join(LAB, "lib", "mcp_lazy.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=r.env, text=True, bufsize=1)
    out = []

    def rd():
        for line in p.stdout:
            out.append(json.loads(line))
    threading.Thread(target=rd, daemon=True).start()

    def ask(mid, method, params=None, wait=10):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
        p.stdin.flush()
        end = time.time() + wait
        while time.time() < end:
            hit = [m for m in out if m.get("id") == mid]
            if hit:
                return hit[0]
            time.sleep(0.02)
        return None
    n0 = len(calls())
    t0 = time.time()
    init = ask(1, "initialize", {"protocolVersion": "2025-06-18"})
    tl = ask(2, "tools/list")
    fast = time.time() - t0
    check("initialize and tools/list come from the cache at once, without the hub or the driver",
          init and "THIS SERVER IS THE VIRTUAL COMPUTER" in init["result"]["instructions"] and len(tl["result"]["tools"]) > 50
          and fast < 2 and len(calls()) == n0 and not r.hub.active_clients(), (fast, len(calls()) - n0))
    res = ask(3, "tools/call", {"name": "whoami", "arguments": {}})
    check("the first tools/call goes through the hub", res and json.loads(res["result"]["content"][0]["text"]).get("driver_pid")
          and len(r.hub.active_clients()) == 1, res)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "slow", "arguments": {"s": 5}}}) + "\n")
    p.stdin.flush()
    time.sleep(0.5)
    r.stop()
    end = time.time() + 5
    while time.time() < end and not [m for m in out if m.get("id") == 4]:
        time.sleep(0.05)
    dropped = [m for m in out if m.get("id") == 4]
    check("the hub going away fails the pending call clearly", dropped and "closed during this call" in json.dumps(dropped[0]), dropped)
    res = ask(5, "tools/call", {"name": "whoami", "arguments": {}}, wait=20)
    check("the next call starts a new hub and goes through it", res and "driver_pid" in json.dumps(res)
          and mcp_hub.hub_pid(r.env) not in (None, os.getpid()), res)
    p.stdin.close()
    p.wait(10)
    mcp_hub.stop_hub(r.env)
    off = dict(r.env, MCOMP_MCP_HUB="off")
    p = subprocess.Popen([PY, os.path.join(LAB, "lib", "mcp_lazy.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=off, text=True, bufsize=1)
    for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}):
        p.stdin.write(json.dumps(m) + "\n")
    p.stdin.flush()
    lines = [json.loads(p.stdout.readline()), json.loads(p.stdout.readline())]
    alone = mcp_hub.hub_pid(r.env) is None
    p.stdin.close()
    p.wait(10)
    check("MCP_HUB=off: the same bridge, on a connection of its own (no hub)", "driver_pid" in json.dumps(lines[1])
          and alone, lines)
    clear_claims()


def cli_checks():
    print("`metacomputer call` output, `metacomputer mcp`")
    r = Running()
    evil = "Title\x1b]52;c;SGVsbG8=\x07\x1b[2J‮evil"
    hub_env = r.env
    import pty
    m, s = pty.openpty()
    p = subprocess.Popen([PY, os.path.join(LAB, "lib", "mcomp_sdk.py"), "call", "echo", json.dumps({"tag": evil})],
                         stdout=s, stderr=s, env=hub_env, close_fds=True)
    os.close(s)
    got = b""
    while True:
        try:
            chunk = os.read(m, 65536)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    p.wait(30)
    os.close(m)
    check("on a terminal, the VM's text is printed escaped (no OSC 52, no screen clear, no bidi override)",
          b"\x1b]52" not in got and b"\x1b[2J" not in got and "‮".encode() not in got and b"\\x1b]52" in got, got[:300])
    raw = subprocess.run([PY, os.path.join(LAB, "lib", "mcomp_sdk.py"), "call", "--raw", "echo", json.dumps({"tag": evil})],
                         env=hub_env, capture_output=True, text=True, timeout=30)
    js = subprocess.run([PY, os.path.join(LAB, "lib", "mcomp_sdk.py"), "call", "--json", "echo", json.dumps({"tag": evil})],
                        env=hub_env, capture_output=True, text=True, timeout=30)
    exact = json.loads(js.stdout)["content"][0]["text"] if js.returncode == 0 else None
    check("--raw prints it exactly; --json is the result exactly", evil in raw.stdout and exact == evil, (raw.stdout[:120], exact))
    check("termsafe is vm_shell's escaping (one helper)", __import__("vm_shell").clean is termsafe.clean)
    r.stop()
    cfg = os.path.join(TD, "none.env")
    open(cfg, "w").write(f'VM_NAME=mcomp-hub-test-none\nRUN_DIR="{os.path.join(TD, "run2")}"\nLOG_DIR="{os.path.join(TD, "logs2")}"\n')
    e = {k: v for k, v in os.environ.items() if not k.startswith("MCOMP_")}
    e["MCOMP_CONFIG"] = cfg
    pr = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "mcp"], input=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n", env=e, capture_output=True, text=True,
        timeout=60)
    check("`metacomputer mcp` without MCOMP_DIRECT_BRIDGE is the guarded bridge, not the raw driver connection",
          "THIS SERVER IS THE VIRTUAL COMPUTER" in pr.stdout, (pr.stdout[:200], pr.stderr[-200:]))
    pr = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "mcp"], input="", env=dict(e, MCOMP_DIRECT_BRIDGE="1"),
                        capture_output=True, text=True, timeout=60)
    check("…and with it, the raw connection (here: the test VM doesn't exist, so it says the VM isn't running)",
          pr.returncode == 1 and "not running" in pr.stderr, pr.stderr[-200:])


def owners_tree_checks():
    print("who: the hub's own connection isn't another controller")
    tree = os.path.join(TD, "tree")
    os.makedirs(os.path.join(tree, "k"), exist_ok=True)
    spawner = "import subprocess, sys\nsubprocess.Popen(sys.argv[1:]).wait()\n"
    for name in ("mcp_hub.py", "mcp_relay.py", "other_client.py"):
        open(os.path.join(tree, name), "w").write(spawner)
    ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ProxyCommand=sleep 40",
           "-o", f"IdentityFile={tree}/k/mcp", "-p", "59994", "cua@127.0.0.1", "true"]
    procs = [subprocess.Popen([PY, os.path.join(tree, "mcp_hub.py"), *ssh], start_new_session=True, stdin=subprocess.DEVNULL),
             subprocess.Popen([PY, os.path.join(tree, "mcp_hub.py"), PY, os.path.join(tree, "mcp_relay.py"), *ssh],
                              start_new_session=True, stdin=subprocess.DEVNULL),
             subprocess.Popen([PY, os.path.join(tree, "other_client.py"), *ssh], start_new_session=True, stdin=subprocess.DEVNULL)]
    try:
        time.sleep(1.5)
        r = subprocess.run([PY, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import owners; "
                                      "print(json.dumps(owners.direct_connections(owners.ps_table())))",
                            os.path.join(LAB, "lib")], capture_output=True, text=True, env=dict(os.environ, MCOMP_SSH_PORT="59994"))
        found = json.loads(r.stdout or "[]")
        chains = [set(d.get("chain") or []) for d in found]
        check("the hub's connection (direct or through the relay) isn't a controller; another client's is",
              len(found) == 1 and procs[2].pid in chains[0], (len(found), r.stderr[-200:]))
    finally:
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()


def main():
    setup_driver()
    try:
        socket_checks()
        multiplex_checks()
        session_checks()
        lifecycle_checks()
        limit_checks()
        owner_checks()
        chrome_checks()
        bounded_checks()
        actionlog_checks()
        coord_checks()
        front_checks()
        cli_checks()
        owners_tree_checks()
    finally:
        mcp_hub.stop_hub(env())
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
