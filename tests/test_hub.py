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
import mcp_client  # noqa: E402
import mcp_hub  # noqa: E402
import mcomp_sdk  # noqa: E402
import owners  # noqa: E402
import termsafe  # noqa: E402
import wirelimits  # noqa: E402

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
        mode = open(os.path.join(STATE, "tools_mode")).read().strip() if os.path.exists(os.path.join(STATE, "tools_mode")) else ""
        if mode == "endless":            # a hostile driver: always one more page
            counter[0] += 1
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": f"t{counter[0]}"}], "nextCursor": f"c{counter[0]}"}})
        elif mode == "repeat":           # ...or the same cursor again
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "t"}], "nextCursor": "same"}})
        else:
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
    elif name == "flood":                # endless tools/list_changed (here: size of them)
        for _ in range(int(a.get("size", 500))):
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        ok(mid, "flooded")
    elif name == "stderrflood":          # stderr without end, the protocol still fine
        chunk = "e" * 65535 + "\n"
        for _ in range(int(a.get("size", 64))):
            sys.stderr.write(chunk)
        sys.stderr.flush()
        ok(mid, "stderr flooded")
    elif name == "badflood":             # valid JSON of the wrong shape, over and over
        for _ in range(int(a.get("size", 100))):
            send({"jsonrpc": "2.0", "method": "notifications/progress", "params": "not an object"})
        ok(mid, "after the bad messages")
    elif name == "deep":                 # a small line nested far deeper than any parser recursion
        out.write("[" * int(a.get("size", 100000)) + "\n"); out.flush()
        out.write('{"jsonrpc":"2.0","id":' + json.dumps(mid) + ',"result":{"deep":' + "[" * 5000 + "]" * 5000 + '}}\n')
        out.flush()
        ok(mid, "after the deep lines")
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
    for n in ("whoami", "echo", "slow", "big", "huge", "nonl", "die", "deaf", "expire", "listchange", "flood",
              "stderrflood", "deep", "badflood"):
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
    bst = os.stat(os.path.dirname(os.path.dirname(lp)))
    check("a RUN_DIR too long for a Unix socket gets a short folder in this user's own temporary folder, not /tmp",
          not lp.startswith(("/tmp/", "/private/tmp/")) and len(lp) < 104 and bst.st_uid == os.getuid()
          and not bst.st_mode & 0o022, lp)
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
    check("(without the hub, MCP_HUB=off, each call has its own driver: the token is unknown, refused before the driver)",
          one.returncode == 0 and two.returncode == 1 and "element_token_unknown" in two.stdout, (two.stdout, two.stderr))
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
    n_keys = len(tool_calls("press_key")) + len(tool_calls("hotkey"))
    res5 = h.call("press_key", {"pid": 50, "window_id": W, "x": 10, "y": 10, "key": "Return"})
    res6 = h.call("hotkey", {"pid": 50, "window_id": W, "x": 10, "y": 10, "keys": ["ctrl", "v"]})
    res7 = h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"press_key": {"key": "End"}},
                                                                    {"hotkey": {"keys": ["ctrl", "a"], "x": 5, "y": 5}}]})
    check("press_key and hotkey with x,y (a focus click there first) are checked too, alone and as a step, no keys sent",
          res5.is_error and res6.is_error and res7.is_error and res7.structured.get("failed_step") == 1
          and res5.structured.get("code") == "windows_changed_since_screenshot"
          and len(tool_calls("press_key")) + len(tool_calls("hotkey")) == n_keys,
          (res5.text[:120], res6.text[:120], res7.text[:120]))
    res8 = h.call("press_key", {"pid": 50, "window_id": W, "key": "Return"})
    res9 = h.call("hotkey", {"pid": 50, "window_id": W, "keys": ["ctrl", "s"]})
    check("press_key and hotkey without x,y aren't pixel input: passed on", not res8.is_error and not res9.is_error,
          (res8.text[:120], res9.text[:120]))
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
    check("the escaping is the shared helper's (termsafe.escape_for_terminal, as in sh, inspect and web)",
          termsafe.escape_for_terminal(evil).encode() in got)
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


# --- what a hostile peer can't do (the verification round) --------------------------------------
def read_until(sock_file, want_id, limit=200):
    for _ in range(limit):
        line = sock_file.readline(1 << 22)
        if not line:
            return None
        try:
            m = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(m, dict) and m.get("id") == want_id:
            return m
    return None


def client_socket_checks():
    print("a client checks the hub's folder and user before it sends anything")
    d = os.path.join(TD, "loose")
    os.makedirs(d)
    os.chmod(d, 0o755)                               # not private: another account's folder would look like this
    path = os.path.join(d, "sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    srv.settimeout(0.5)
    try:
        mcp_hub._connect(path)
        refused = None
    except mcp_hub.HubError as e:
        refused = str(e)
    try:
        srv.accept()[0].close()
        reached = True
    except socket.timeout:
        reached = False
    check("a hub folder that isn't private to this user is refused before connecting", refused
          and "isn't a private folder" in refused and not reached, refused)
    os.chmod(d, 0o700)
    real = mcp_hub.peer_cred
    mcp_hub.peer_cred = lambda s: (os.getuid() + 1, 4242)    # as if another account were serving the socket
    try:
        try:
            mcp_hub._connect(path)
            refused = None
        except mcp_hub.HubError as e:
            refused = str(e)
    finally:
        mcp_hub.peer_cred = real
    conn = srv.accept()[0]
    conn.settimeout(2)
    try:
        data = conn.recv(100)
    except socket.timeout:
        data = b"(still open)"
    conn.close()
    srv.close()
    check("a socket served by another user id is refused before a byte is sent (no hello, no tool call)",
          refused and "nothing was sent" in refused and data == b"", (refused, data))
    e = env(MCOMP_RUN_DIR=os.path.join(TD, "run-loose"))
    sd = mcp_hub.paths(e)["sockdir"]
    os.makedirs(sd, exist_ok=True)
    os.chmod(sd, 0o755)
    try:
        mcp_hub.connect_or_start(e, timeout=5).close()
        started = "connected"
    except mcp_hub.HubError as ex:
        started = str(ex)
    check("…and a client doesn't start or reach a hub behind such a folder", "private folder" in started
          and mcp_hub.hub_pid(e) is None, started)
    if not sd.startswith(TD):
        shutil.rmtree(sd, ignore_errors=True)
    # A hub started by an earlier version listens where that version put its socket; its hub.pid says where.
    r = Running()
    p = mcp_hub.paths(r.env)
    alt_dir = os.path.join(TD, "older-hub")
    os.makedirs(alt_dir)
    os.chmod(alt_dir, 0o700)
    alt = os.path.join(alt_dir, "sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(alt)
    srv.listen(2)
    srv.settimeout(2)
    rec = json.load(open(p["pid"]))
    with open(p["pid"], "w") as f:
        json.dump(dict(rec, socket=alt), f)
    try:
        s = mcp_hub.connect_or_start(r.env, start=False)
        srv.accept()[0].close()
        s.close()
        reached = True
    except (OSError, mcp_hub.HubError) as ex:
        reached = repr(ex)
    finally:
        with open(p["pid"], "w") as f:
            json.dump(rec, f)
        srv.close()
    check("a hub listening where its own record says (an earlier version's folder) is found, with the same checks",
          reached is True and mcp_hub.hub_sockets(r.env) == [p["sock"]], reached)
    r.stop()


def refresh_checks():
    print("the driver's tool list: one refresh at a time, capped")
    saved = (mcp_hub.REFRESH_GAP_S, mcp_hub.MAX_TOOL_PAGES)
    mcp_hub.REFRESH_GAP_S = 0.5
    mode = os.path.join(STATE, "tools_mode")
    try:
        r = Running()
        got = []
        raw = mcomp_sdk.open_connection(r.env, kind="sdk")
        raw.on_message = got.append
        raw.start()
        h = client(r)
        h.call("whoami", {})

        def n_lists():
            return len([c for c in calls() if c["msg"].get("method") == "tools/list"])
        n0, threads0 = n_lists(), threading.active_count()
        res = h.call("flood", {"size": 2000})
        peak = threading.active_count()
        time.sleep(2.0)
        notes = [m for m in got if m.get("method") == "notifications/tools/list_changed"]
        check("2,000 tools/list_changed notices: at most two refreshes, and no thread per notice",
              not res.is_error and 1 <= n_lists() - n0 <= 2 and peak - threads0 < 5, (n_lists() - n0, peak - threads0))
        check("…and the clients hear of it once per refresh, not 2,000 times", 1 <= len(notes) <= 2, len(notes))
        old = list(r.hub.live_tools)
        mcp_hub.MAX_TOOL_PAGES = 5
        with open(mode, "w") as f:
            f.write("endless")
        n0 = n_lists()
        r.hub.refresh_cache(mcp_hub.DEFAULT_INIT)
        check("a driver that always sends one more page is cut off at the page limit; the earlier list stays",
              n_lists() - n0 == 6 and r.hub.live_tools == old, (n_lists() - n0, len(r.hub.live_tools)))
        with open(mode, "w") as f:
            f.write("repeat")
        n0 = n_lists()
        r.hub.refresh_cache(mcp_hub.DEFAULT_INIT)
        check("…and one that sends a cursor again is cut off then", n_lists() - n0 == 2 and r.hub.live_tools == old,
              n_lists() - n0)
        raw.close()
        h.close()
        r.stop()
    finally:
        mcp_hub.REFRESH_GAP_S, mcp_hub.MAX_TOOL_PAGES = saved
        if os.path.exists(mode):
            os.remove(mode)


def stderr_checks():
    print("the driver's stderr can't fill the Mac's disk or memory")
    saved = wirelimits.STDERR_FILE_MAX
    wirelimits.STDERR_FILE_MAX = 256 << 10
    try:
        r = Running()
        h = client(r)
        res = h.call("stderrflood", {"size": 64})        # 4 MB on stderr
        time.sleep(0.5)
        size = os.path.getsize(mcp_hub.paths(r.env)["stderr"])
        tail = r.hub.stderr_tail.text() if r.hub.stderr_tail else ""
        check("4 MB on the driver's stderr: the call is answered and the log file stops at its cap",
              not res.is_error and (256 << 10) <= size < (256 << 10) + 200, size)
        check("…and only a short tail of it is kept in memory for error messages",
              0 < len(tail) <= wirelimits.STDERR_KEEP and r.hub.stderr_tail.total >= 4 << 20, len(tail))
        h.close()
        r.stop()
    finally:
        wirelimits.STDERR_FILE_MAX = saved


def deep_checks():
    print("deeply nested JSON from the driver or a client")

    def refused(data):
        try:
            wirelimits.loads(data)
            return False
        except ValueError:
            return True
    nested = lambda n: "[" * n + "]" * n  # noqa: E731
    check("wirelimits.loads: a line of '[' and anything over the depth limit are a ValueError, never RecursionError",
          refused(b"[" * 100000) and refused(nested(wirelimits.MAX_JSON_DEPTH + 1))
          and wirelimits.loads(nested(50)) == json.loads(nested(50)))
    r = Running()
    h = client(r)
    res = h.call("deep", {})
    again = h.call("whoami", {})
    check("the hub skips a 100,000-deep line and a 5,000-deep answer from the driver; its reader lives on",
          not res.is_error and "after the deep lines" in res.text and again.structured.get("driver_pid"), res.text[:200])
    s = mcp_hub.connect_or_start(r.env, start=False)
    f = s.makefile("rb")
    s.sendall((json.dumps({"jsonrpc": "2.0", "id": 0, "method": "metacomputer/hello",
                           "params": {"protocol": 1, "kind": "sdk"}}) + "\n").encode())
    s.sendall(b"[" * 200000 + b"\n")
    s.sendall((json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "whoami", "arguments": {}}}) + "\n").encode())
    s.settimeout(10)
    ans = read_until(f, 7)
    s.close()
    check("a client that sends one gets a parse error and stays connected", ans and "result" in ans, ans)
    h.close()
    r.stop()
    adir, key = os.path.join(TD, "actions-deep"), os.path.join(TD, "keys", "alog-deep")
    p = subprocess.Popen([PY, os.path.join(LAB, "lib", "mcp_relay.py"), PY, FAKE, STATE], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env=dict(env(), MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=adir, MCOMP_ACTION_LOG_KEY=key))
    for i, name in ((1, "deep"), (2, "whoami")):
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                   "params": {"name": name, "arguments": {}}}) + "\n").encode())
    p.stdin.flush()
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("m", read_until(p.stdout, 2)), daemon=True)
    th.start()
    th.join(15)
    p.stdin.close()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
    check("the action-log relay passes them on and keeps relaying (no dead reader)", box.get("m") and "result" in box["m"],
          box.get("m"))
    c = mcp_client.MCPClient(cmd=[PY, FAKE, STATE])
    c.start(timeout=20)
    r1, r2 = c.call("deep", {}, timeout=20), c.call("whoami", {}, timeout=20)
    c.close()
    check("…and so does the direct client", not r1.is_error and "after the deep lines" in r1.text and not r2.is_error,
          (r1.text[:100], r2.text[:100]))


def transport_checks():
    print("the raw driver connection is for transport tests only; the direct client's backlog is capped")
    c = mcp_client.MCPClient(cmd=[os.path.join(LAB, "bin", "metacomputer"), "mcp"])
    try:
        c.start()
        err = None
    except mcp_client.MCPError as e:
        err = str(e)
    check("MCPClient won't open `metacomputer mcp` (MCOMP_DIRECT_BRIDGE) unless a transport test asks for it",
          err and "transport tests" in err and c.proc is None, err)
    src = {n: open(os.path.join(LAB, "lib", n)).read() for n in ("harness.py", "browser_tools.py", "mcp_client.py")}
    check("the GUI test harness and `web` use the hub's SDK, not the raw client",
          "MCPClient" not in src["harness.py"] and "MCPClient" not in src["browser_tools.py"]
          and "mcomp_sdk.Hub(" in src["harness.py"] and "mcomp_sdk.Hub(" in src["browser_tools.py"])
    saved = mcp_client.MAX_QUEUED_BYTES
    mcp_client.MAX_QUEUED_BYTES = 1_000_000
    try:
        c = mcp_client.MCPClient(cmd=[PY, FAKE, STATE])
        c.start(timeout=20)
        for i in range(10):                          # 2 MB of answers nobody reads
            c._send({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                     "params": {"name": "big", "arguments": {"size": 200_000}}})
        end = time.time() + 10
        while time.time() < end and c.proc.poll() is None:
            time.sleep(0.1)
        check("the direct client drops the connection once more than its byte budget waits unread",
              c.closed_why and "MB" in c.closed_why and c.proc.poll() is not None, c.closed_why)
        c.close()
    finally:
        mcp_client.MAX_QUEUED_BYTES = saved


def web_checks():
    print("`web` and the GUI tests: clients of the hub that claim before they start")
    clear_claims()
    r = Running()
    old = os.environ.get("MCOMP_HUB_DRIVER_CMD")
    os.environ["MCOMP_HUB_DRIVER_CMD"] = r.env["MCOMP_HUB_DRIVER_CMD"]
    import browser_tools
    other = foreign_claim()
    try:
        d = browser_tools.Driver()
        kinds = [c.kind for c in r.hub.active_clients()]
        try:
            d.claim("web test")
            refused = None
        except browser_tools.Fail as e:
            refused = str(e)
        check("web is a client of the hub (kind web); its claim is refused while another session acts",
              "web" in kinds and refused and "/another/project" in refused and "--override" in refused, (kinds, refused))
        d.claim("web test", override=True)
        mine = json.load(open(owners.claim_path(os.getpid()))) if os.path.exists(owners.claim_path(os.getpid())) else {}
        check("--override releases the other session's claim (as `metacomputer release`), then claims through the hub",
              not os.path.exists(owners.claim_path(other.pid)) and mine.get("last_tool") == "web test", mine)
        d.close()
        time.sleep(0.3)
        check("…and closing lets go of the claim", not os.path.exists(owners.claim_path(os.getpid())))
        # Two separate controllers: detached from this process (and any Claude Code above it, which would make
        # them one session), started at the same moment.
        script = ("import json, os, sys, time; sys.path.insert(0, sys.argv[1]); import mcomp_sdk\n"
                  "if os.fork(): os._exit(0)\n"
                  "os.setsid()\n"
                  "while os.getppid() != 1: time.sleep(0.01)\n"
                  "go = float(sys.argv[4])\n"
                  "while time.time() < go: time.sleep(0.002)\n"
                  "h = mcomp_sdk.Hub(kind=sys.argv[2]).connect(); r = h.claim('race')\n"
                  "open(sys.argv[3], 'w').write(json.dumps(r)); time.sleep(2); h.close()\n")
        go = time.time() + 1.5
        outs_p = [os.path.join(TD, f"race-{kind}.json") for kind in ("web", "test")]
        for kind, outp in zip(("web", "test"), outs_p):
            subprocess.run([PY, "-c", script, os.path.join(LAB, "lib"), kind, outp, str(go)], env=dict(os.environ),
                           timeout=30)
        end = time.time() + 30
        while time.time() < end and not all(os.path.exists(p) and os.path.getsize(p) for p in outs_p):
            time.sleep(0.1)
        outs = [json.load(open(p)) if os.path.exists(p) else {} for p in outs_p]
        won = [o.get("claimed") for o in outs]
        check("two runs started together (web and a GUI test): exactly one gets the computer", won.count(True) == 1, outs)
        time.sleep(2.5)
    finally:
        drop_foreign(other)
        if old is None:
            os.environ.pop("MCOMP_HUB_DRIVER_CMD", None)
        else:
            os.environ["MCOMP_HUB_DRIVER_CMD"] = old
        clear_claims()
        r.stop()


def lease_checks():
    print("calls a client left running when it disconnected keep the computer claimed")
    clear_claims()
    r = Running()
    h = mcomp_sdk.Hub(env=r.env, kind="sdk", timeout=1).connect()
    res = h.call("slow", {"s": 3})                   # times out here; the driver is still at it
    h.close()
    time.sleep(0.3)
    leases = [c for c in owners.claims() if c.get("kind") == "lease"]
    plain = [c for c in owners.claims() if c.get("kind") != "lease"]
    stranger = mcp_hub.Client(r.hub, None, 99999)    # another controller: not this process, no Claude Code
    stranger.mine, stranger.claude, stranger.holder = set(), None, 99999
    busy = r.hub.busy_others(stranger)
    check("a script that times out and closes: its claim becomes the hub's lease while the call runs",
          "no answer" in res.text and len(leases) == 1 and leases[0]["bridge_pid"] == r.hub.pid
          and leases[0]["last_tool"] == "slow" and leases[0].get("client_pid") == os.getpid() and not plain,
          (res.text[:80], leases, plain))
    check("…so another controller's acting call is refused meanwhile, and `who` says why",
          any(o.get("claim") == "lease" for o in busy) and "left running" in owners.describe(busy[0]), busy)
    same = mcp_hub.Client(r.hub, None, os.getpid())
    same.mine, same.claude, same.holder = {os.getpid()}, None, os.getpid()
    check("…but not the same process reconnecting", not [o for o in r.hub.busy_others(same) if o.get("claim") == "lease"])
    end = time.time() + 8
    while time.time() < end and [c for c in owners.claims() if c.get("kind") == "lease"]:
        time.sleep(0.1)
    check("once the call ends, the lease ends and nothing else holds the computer",
          not [c for c in owners.claims() if c.get("kind") == "lease"] and not r.hub.leases and not r.hub.busy_others(stranger))
    # The hand-over happens under the owners lock: while another process holds it, neither the lease nor the drop
    h = mcomp_sdk.Hub(env=r.env, kind="sdk", timeout=1).connect()
    h.call("slow", {"s": 3})
    flag = threading.Event()

    def holder():
        with owners.lock():
            flag.set()
            time.sleep(1.0)
    th = threading.Thread(target=holder)
    th.start()
    flag.wait(2)
    h.close()
    time.sleep(0.4)
    # (Here the hub runs in this process, so its lease and the client's claim are the same file.)
    during = [c.get("kind") for c in owners.claims()]
    th.join()
    time.sleep(0.4)
    after = [c.get("kind") for c in owners.claims()]
    check("the claim hand-over waits for the owners lock, and the claim is never missing in between",
          during == [None] and after == ["lease"], (during, after))
    end = time.time() + 8
    while time.time() < end and r.hub.leases:
        time.sleep(0.1)
    st = mcp_hub.describe_status(r.hub.status())
    check("the hub's status counts what's left running (none now)", "left running" not in st, st)
    r.stop()
    clear_claims()


def token_checks():
    print("element tokens: the Chrome guard knows which app a token came from")
    r = Running()
    r.hub.input_reset = lambda: None
    h = client(r, "call", session="tok")
    ch = h.call("get_window_state", {"pid": 77, "window_id": CHROME}).structured.get("snapshot_id")
    mp = h.call("get_window_state", {"pid": 50, "window_id": W}).structured.get("snapshot_id")
    n0 = len(calls())
    sv = h.call("set_value", {"element_token": f"{ch}:1", "value": "typed"})
    ck = h.call("click", {"element_token": f"{ch}:2"})
    tt = h.call("type_text", {"element_token": f"{ch}:3", "text": "x"})
    rs = h.call("run_steps", {"steps": [{"set_value": {"element_token": f"{ch}:1", "value": "y"}}]})
    other = h.call("click", {"element_token": f"{mp}:2"})
    unknown = h.call("set_value", {"element_token": "s0000ffff:1", "value": "z"})
    unknown2 = h.call("click", {"element_token": "s0000fffe:1"})
    sent = [c["msg"]["params"] for c in tool_calls(None, n0)]
    clicks = [p["arguments"] for p in sent if p["name"] in ("click", "type_text")]
    check("set_value by a Chrome token alone is refused, alone and as a run_steps step, before the driver",
          sv.is_error and "browser_type" in sv.text and rs.is_error and "browser_type" in rs.text
          and not [p for p in sent if p["name"] in ("set_value", "run_steps")], (sv.text[:80], rs.text[:80]))
    check("input by a Chrome token alone goes in the foreground", not ck.is_error and not tt.is_error
          and [a.get("delivery_mode") for a in clicks[:2]] == ["foreground", "foreground"], clicks)
    check("a token from another app's read passes as it was", not other.is_error and clicks[-1].get("delivery_mode") is None,
          clicks[-1:])
    check("a token from a read the hub never saw is refused when that's all there is (code element_token_unknown)",
          unknown.is_error and unknown.structured.get("code") == "element_token_unknown" and unknown2.is_error
          and len([p for p in sent if p["name"] == "click"]) == 2, (unknown.text[:120], unknown2.text[:80]))
    for i in range(mcp_hub.MAX_SNAPSHOTS + 50):
        r.hub.learn_apps({"snapshot_id": f"s{i:08x}", "pid": 1000 + i, "window_id": i, "app_name": "x"})
    check("what the hub remembers from results is capped (snapshots, windows, apps)",
          len(r.hub.snapshots) == mcp_hub.MAX_SNAPSHOTS and len(r.hub.window_pid) <= mcp_hub.MAX_WINDOW_PIDS
          and len(r.hub.app_of) <= mcp_hub.MAX_APPS, (len(r.hub.snapshots), len(r.hub.app_of)))
    r.hub.learn_apps({"steps": [{"pid": 4242, "name": "Reserve"}]}, by_name=False)
    check("an element's name in a batch result is never taken for its app's name", 4242 not in r.hub.app_of)
    st = mcomp_sdk.Hub(env=r.env, kind="status", timeout=20).connect()
    ro = st.call("press_key", {"pid": 50, "window_id": W, "key": "a"})
    ok_read = st.call("list_windows", {})
    st.close()
    check("a status connection (the probe) can read but never act or claim", ro.is_error and "can only read" in ro.text
          and not ok_read.is_error, ro.text[:120])
    raw = mcomp_sdk.open_connection(r.env, kind="sdk")
    got = []
    raw.on_message = got.append
    raw.start()
    raw.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
              "params": {"name": "whoami", "arguments": {}, "_meta": {"progressToken": "p1"}}})
    end = time.time() + 5
    while time.time() < end and not [m for m in got if m.get("id") == 9]:
        time.sleep(0.05)
    check("a request's progress token is forgotten when it's answered", not r.hub.progress, r.hub.progress)
    raw.close()
    h.close()
    clear_claims()
    r.stop()


def batch_checks():
    print("the coordinate guard inside a run_steps batch")
    clear_claims()
    set_windows(BASE_WINDOWS)
    r = Running()
    h = client(r, "call", session="batch")
    h.call("get_window_state", {"pid": 50, "window_id": W})
    n0 = len(tool_calls("run_steps"))

    def batch(*steps):
        return h.call("run_steps", {"pid": 50, "window_id": W, "steps": list(steps)})
    refused = {
        "super+Up": batch({"hotkey": {"keys": ["super", "Up"]}}, {"click": {"x": 10, "y": 10}}),
        "F11": batch({"press_key": {"key": "F11"}}, {"click": {"x": 10, "y": 10}}),
        "ctrl+s": batch({"hotkey": {"keys": ["ctrl", "s"]}}, {"type_text": {"x": 10, "y": 10, "text": "a"}}),
        "double click": batch({"double_click": {"x": 5, "y": 5}}, {"click": {"x": 10, "y": 10}}),
        "drag": batch({"drag": {"from_x": 1, "from_y": 1, "to_x": 50, "to_y": 50}}, {"right_click": {"x": 10, "y": 10}}),
        "desktop press": batch({"click": {"x": 900, "y": 5, "scope": "desktop"}}, {"click": {"x": 10, "y": 10}}),
        "alt+tab": batch({"hotkey": {"keys": "alt+tab"}}, {"tool": "click", "args": {"x": 10, "y": 10}}),
    }
    bad = {k: (v.text[:100], v.structured) for k, v in refused.items()
           if not (v.is_error and v.structured.get("code") == "pixels_after_window_change"
                   and v.structured.get("failed_step") == 1 and v.structured.get("window_step") == 0
                   and "take a fresh screenshot" in v.structured.get("hint", ""))}
    check("a pixel step after a step that can move, resize, open or close windows is refused before the batch runs",
          not bad and len(tool_calls("run_steps")) == n0, bad)
    ok = {
        "name box": batch({"click": {"x": 55, "y": 94}}, {"hotkey": {"keys": ["ctrl", "a"]}}, {"type_text": {"text": "A1"}},
                          {"press_key": {"key": "Return"}}, {"click": {"x": 55, "y": 94}}),
        "pixels first": batch({"click": {"x": 10, "y": 10}}, {"hotkey": {"keys": ["super", "Up"]}}),
        "no pixels after": batch({"hotkey": {"keys": ["ctrl", "s"]}}, {"click": {"role": "button", "name": "Save"}}),
    }
    check("ordinary batches still pass: clicks, typing, Return and Ctrl+A between pixel steps; pixels before; "
          "role/name targets after", all(not v.is_error for v in ok.values()) and len(tool_calls("run_steps")) == n0 + 3,
          {k: v.text[:100] for k, v in ok.items() if v.is_error})
    h.close()
    r.stop()
    r = Running(MCOMP_COORD_GUARD="off")
    h = client(r, "call", session="batch")
    res = h.call("run_steps", {"pid": 50, "window_id": W, "steps": [{"hotkey": {"keys": ["super", "Up"]}},
                                                                    {"click": {"x": 10, "y": 10}}]})
    check("COORD_GUARD=off: not checked", not res.is_error, res.text[:100])
    h.close()
    r.stop()
    clear_claims()


def who_checks():
    print("`who` lists every kind of claim")
    clear_claims()
    now = time.time()
    owners.write_claim(os.getpid(), None, "/a/project", "sh", now, kind="shell", sid=12345)
    owners.write_claim(os.getppid(), None, "/b/project", "slow", now, kind="lease", extra={"client_pid": 4321})
    p = subprocess.run([PY, os.path.join(LAB, "lib", "owners.py"), "who"], capture_output=True, text=True,
                       env=dict(os.environ), timeout=60)
    check("a shell claim and a lease are described, not mistaken for direct connections", p.returncode == 0
          and "metacomputer sh outside Claude Code" in p.stdout and "left running" in p.stdout, (p.stdout, p.stderr[-300:]))
    clear_claims()


# --- second verification round ---------------------------------------------------------------
def memory_checks():
    print("N2: what a small frame or many screenshots can make the hub hold")
    import tracemalloc
    wide = [[] for _ in range(300_000)]
    tracemalloc.start()
    deep = wirelimits.depth_exceeds(wide, 100)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("the depth check's stack is as deep as the JSON, not as wide (300,000 siblings: under 1 MB)",
          not deep and peak < 1_000_000, peak)
    many = b"[" + b"[]," * 2_000_000 + b"[]]"
    tracemalloc.start()
    try:
        wirelimits.loads(many)
        refused = False
    except ValueError as e:
        refused = "values" in str(e)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("a frame with millions of values is refused before it's parsed (no millions of objects)",
          refused and peak < 10_000_000, peak)
    hub = mcp_hub.Hub(env=env(MCOMP_RUN_DIR=os.path.join(TD, "run-mem")))
    junk = "j" * 1000
    for i in range(300):
        ws = [{"window_id": 1000 * i + k, "pid": 5, "app_name": "A" * 500, "z_index": k, "junk": junk,
               "bounds": {"x": 0, "y": 0, "width": 100, "height": 100, "extra": junk}} for k in range(400)]
        res = {"content": [{"type": "image"}]}
        sc = {"windows": ws, "screen_width": 1920, "screen_height": 1080, "screenshot_width": 1920,
              "screenshot_height": 1080}
        hub.note_layout("get_desktop_state", res, sc, f"s{i}", {}, None)
    per = [len(r["windows"] or []) for r in hub.layouts.values()]
    keys = {k for r in hub.layouts.values() for w in (r["windows"] or []) for k in w}
    names = {len(w["app_name"]) for r in hub.layouts.values() for w in (r["windows"] or [])}
    check("screenshot layouts keep only checked geometry, at most 256 windows each and 32,768 in all",
          max(per) <= mcp_hub.MAX_LAYOUT_WINDOWS and sum(per) <= mcp_hub.MAX_LAYOUT_WINDOWS_TOTAL
          and keys <= {"window_id", "z_index", "bounds", "is_on_screen", "minimized", "app_name"} and max(names) <= 40,
          (max(per), sum(per), keys))
    hub.note_layout("get_desktop_state", {"content": []}, {"windows": [{"window_id": 1, "bounds": {"x": "a"}}],
                    "screen_width": float("inf"), "screen_height": 1}, "bad", {}, None)
    rec = hub.layouts.get(("bad", "desktop"))
    check("…and a frame that isn't finite numbers isn't kept at all", rec and rec["bounds"] is None and rec["windows"] == [],
          rec)


def log_checks():
    print("N3: a driver that keeps sending unusable messages can't fill hub.log")
    saved = (mcp_hub.BAD_UPSTREAM_MAX, mcp_hub.LOG_MAX)
    mcp_hub.BAD_UPSTREAM_MAX = 20
    try:
        r = Running()
        h = client(r)
        h.call("whoami", {})
        log = mcp_hub.paths(r.env)["log"]
        before = os.path.getsize(log)
        res = h.call("badflood", {"size": 200})
        time.sleep(0.5)
        grew = os.path.getsize(log) - before
        lines = [x for x in open(log).read().splitlines() if "couldn't use" in x or "error handling" in x]
        check("200 messages of the wrong shape: a handful of log lines, not one each", grew < 3000 and len(lines) <= 10,
              (grew, len(lines)))
        check("…and past the limit the hub hangs up on the driver (the call fails clearly; the next call reconnects)",
              res.is_error and "can't use" in res.text and not h.call("whoami", {}).is_error, res.text[:160])
        h.close()
        mcp_hub.LOG_MAX = 4000
        for i in range(300):
            r.hub.note(f"line {i} " + "x" * 60)
        size = os.path.getsize(log)
        check("hub.log is rotated while the hub runs, not only at start", size <= 4200 and os.path.exists(log + ".1"),
              size)
        r.stop()
    finally:
        mcp_hub.BAD_UPSTREAM_MAX, mcp_hub.LOG_MAX = saved


def lease2_checks():
    print("N4/N5: a lease lasts as long as the calls it holds the computer for")
    clear_claims()
    saved = (owners.ACTIVE_S, mcp_hub.LEASE_TICK_S, mcp_hub.LEASE_MAX_S)
    owners.ACTIVE_S, mcp_hub.LEASE_TICK_S, mcp_hub.LEASE_MAX_S = 2, 0.2, 30
    try:
        r = Running()
        h = mcomp_sdk.Hub(env=r.env, kind="sdk", timeout=1).connect()
        h.call("slow", {"s": 5})
        h.close()
        time.sleep(3.5)                              # past the activity window (2 s here)
        leases = [c for c in owners.claims() if c.get("kind") == "lease"]
        check("a lease stays active past the activity window while its call runs (it's kept fresh)",
              leases and leases[0]["active"], leases)
        end = time.time() + 6
        while time.time() < end and r.hub.leases:
            time.sleep(0.1)
        mcp_hub.LEASE_MAX_S = 2
        h = mcomp_sdk.Hub(env=r.env, kind="sdk", timeout=1).connect()
        drv = h.call("whoami", {}).structured["driver_pid"]
        h.call("slow", {"s": 60})
        h.close()
        end = time.time() + 5
        while time.time() < end and not [c for c in owners.claims() if c.get("kind") == "lease"]:
            time.sleep(0.05)
        t0 = time.time()
        while time.time() - t0 < 10 and [c for c in owners.claims() if c.get("kind") == "lease"]:
            time.sleep(0.1)
        gone_at = time.time() - t0
        check("past the lease's longest life the hub ends the driver runtime first, then lets the computer go",
              1 < gone_at < 8 and not owners.alive(drv), (gone_at, owners.alive(drv)))
        mcp_hub.LEASE_MAX_S = 30
        h = mcomp_sdk.Hub(env=r.env, kind="call", timeout=1).connect()
        h.call("slow", {"s": 3})
        h.close()
        time.sleep(0.4)
        leases = [c for c in owners.claims() if c.get("kind") == "lease"]
        check("a one-shot `call` held by its Claude Code session or shell gets a lease for a call it left running too",
              leases and leases[0].get("via") == "call", leases)
        end = time.time() + 6
        while time.time() < end and r.hub.leases:
            time.sleep(0.1)
        # the decision about a lease is made under the lock the hand-over assigns it under
        pend = {"client": None}
        done = threading.Event()
        r.hub.lease_lock.acquire()
        th = threading.Thread(target=lambda: (r.hub._pend_finished("k-race", pend), done.set()), daemon=True)
        th.start()
        time.sleep(0.3)
        waited = not done.is_set()
        pend["lease"] = 4242                         # the hand-over assigns it in that window
        r.hub.leases[4242] = {"keys": {"k-race"}, "tool": "slow", "claude": None, "cwd": "/", "client_pid": 1,
                              "kind": "sdk", "t0": time.monotonic(), "written": True, "refreshed": time.monotonic(),
                              "drop_pid": None}
        r.hub.lease_lock.release()
        th.join(3)
        time.sleep(0.5)
        check("a call finishing during a hand-over can't leave a lease waiting for it", waited and 4242 not in r.hub.leases,
              (waited, list(r.hub.leases)))
        # ownership follows the action even when the claim file is gone
        h = mcomp_sdk.Hub(env=r.env, kind="sdk", timeout=20).connect()
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("r", h.call("slow", {"s": 2})), daemon=True)
        th.start()
        time.sleep(0.4)
        clear_claims()                               # as if its holder had exited
        stranger = mcp_hub.Client(r.hub, None, 99999)
        stranger.mine, stranger.claude, stranger.holder = set(), None, 99999
        busy = r.hub.busy_others(stranger)
        check("an acting call another controller has running counts as busy even without its claim file",
              any(o.get("claim") == "pending" for o in busy) and "still running" in owners.describe(
                  next(o for o in busy if o.get("claim") == "pending")), busy)
        th.join(5)
        h.close()
        r.stop()
    finally:
        owners.ACTIVE_S, mcp_hub.LEASE_TICK_S, mcp_hub.LEASE_MAX_S = saved
        clear_claims()


def lease_write_checks():
    print("N4: a lease that can't be written keeps the client's claim")
    clear_claims()
    proc = subprocess.Popen([PY, "-c", (
        "import json, os, sys, time; sys.path.insert(0, sys.argv[1]); import mcomp_sdk, mcp_hub\n"
        "h = mcomp_sdk.Hub(env=json.loads(sys.argv[2]), kind='sdk', timeout=1).connect()\n"
        "h.call('slow', {'s': 4}); h.close(); print('closed', flush=True); time.sleep(6)\n"),
        os.path.join(LAB, "lib"), json.dumps(env())], stdout=subprocess.PIPE, text=True)
    r = None
    real = owners.write_claim
    try:
        r = Running()
        # (the client's claim is its own; only the hub's lease write fails)
        owners.write_claim = lambda pid, *a, **k: (_ for _ in ()).throw(OSError("disk full")) if k.get("kind") == "lease" \
            else real(pid, *a, **k)
        proc.stdout.readline()
        time.sleep(0.5)
        kept = os.path.exists(owners.claim_path(proc.pid))
        owners.write_claim = real
        time.sleep(2.0)                              # the keeper writes the lease, then the old claim goes
        leases = [c for c in owners.claims() if c.get("kind") == "lease"]
        check("the client's claim stays while the lease can't be written, and is replaced once it can",
              kept and leases and not os.path.exists(owners.claim_path(proc.pid)), (kept, leases))
    finally:
        owners.write_claim = real
        proc.kill()
        proc.wait()
        if r:
            r.stop()
        clear_claims()


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
        client_socket_checks()
        refresh_checks()
        stderr_checks()
        deep_checks()
        transport_checks()
        web_checks()
        lease_checks()
        token_checks()
        batch_checks()
        who_checks()
        memory_checks()
        log_checks()
        lease2_checks()
        lease_write_checks()
    finally:
        mcp_hub.stop_hub(env())
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
