#!/usr/bin/env python3
"""`metacomputer claude` against a running test VM, end to end, with the real Claude Code:

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_vm_claude_vm.py

- A TEST build of the vault (scripts/build-vault.sh --test) with its own folder in /tmp, bound to the test VM.
  Its proxy's upstream is a stand-in Anthropic API on this Mac (MCOMP_VAULT_TEST_API_HTTP=1: plain http to
  127.0.0.1), so no real key or account is used. Two api secrets hold dummy values: one sent as
  Authorization: Bearer (a subscription token's way), one as x-api-key (an API key's way).
- The stand-in API answers POST /v1/messages (streamed or not) only when the dummy key the proxy put in is
  right, and records what it got: the path, the auth header, anthropic-beta, the last user message.
- In the VM, Claude Code is removed first (with its settings), so `metacomputer claude` installs it with
  Anthropic's native installer (it needs the internet) and sets its first-run choices from nothing.

Checked: install, first-run choices, a session in tmux with a terminal on the desktop, send (one line, many
lines, --no-enter + keys), read, wait, status, both header kinds, --skip-permissions, stop with and without
--revoke, the proxy refusing a revoked token; and that the dummy keys are nowhere in the VM, while the token
is in no Mac command line, log, event, state file or command output.

A test VM only: it removes Claude Code and its settings from the VM, and leaves the VM running.
"""
import hashlib
import http.server
import json
import os
import re
import secrets as pysecrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402

LAB = ts.LAB
MC = os.path.join(LAB, "bin", "metacomputer")
check = ts.check
PASS = ts.PASS
DUMMY_SUB = "sk-ant-oat01-dummy-" + pysecrets.token_hex(16)     # stands in for a `claude setup-token` value
DUMMY_KEY = "sk-ant-api03-dummy-" + pysecrets.token_hex(16)     # stands in for an API key
NONCE = pysecrets.token_hex(4)
FIRST_RUN = ("Choose the text style", "Do you trust the files", "Select login method", "Detected a custom API key",
             "Yes, I accept", "Press Enter to continue", "Security notes")


class FakeAPI(http.server.BaseHTTPRequestHandler):
    got = []
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self._send(200, b"")

    def do_GET(self):
        FakeAPI.got.append({"method": "GET", "path": self.path, "auth": self._auth()})
        self._send(404, json.dumps({"type": "error", "error": {"type": "not_found_error", "message": "not here"}}))

    def _auth(self):
        a, k = self.headers.get("Authorization") or "", self.headers.get("x-api-key") or ""
        if a == "Bearer " + DUMMY_SUB:
            return "bearer-sub"
        if k == DUMMY_KEY:
            return "x-api-key"
        return "bad:" + hashlib.sha256((a + "|" + k).encode()).hexdigest()[:12]

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            req = {}
        last = ""
        for m in req.get("messages") or []:
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    last = c
                elif isinstance(c, list):
                    texts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
                    texts = [t for t in texts if not t.startswith("<system-reminder>")]
                    last = texts[-1] if texts else last
        rec = {"method": "POST", "path": self.path, "auth": self._auth(), "beta": self.headers.get("anthropic-beta") or "",
               "stream": bool(req.get("stream")), "model": req.get("model"), "last": last}
        FakeAPI.got.append(rec)
        if rec["auth"].startswith("bad"):
            return self._send(401, json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                                           "message": "invalid key (stand-in API)"}}))
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self._send(200, json.dumps({"input_tokens": 12}))
        if path != "/v1/messages":
            return self._send(404, json.dumps({"type": "error", "error": {"type": "not_found_error", "message": path}}))
        if "SLOWSTREAM" in last and rec["stream"]:
            return self._slow(req, rec)
        text = f"PONG-{NONCE}" if "PING" in last else "fake reply"
        if "PING" in last and "\n" in last:
            text += " lines=" + str(last.count("\n") + 1)
        if not rec["stream"]:
            return self._send(200, json.dumps({"id": "msg_fake", "type": "message", "role": "assistant", "model": req.get("model"),
                                               "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                               "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 3}}))
        events = [
            ("message_start", {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant",
                                                                     "model": req.get("model"), "content": [], "stop_reason": None,
                                                                     "stop_sequence": None,
                                                                     "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"})]
        data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
        self._send(200, data, "text/event-stream")

    def _slow(self, req, rec):
        """An answer streamed a word every half second for 40 s; rec["cut"] says whether the far end hung up first."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def ev(e, d):
            self.wfile.write(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode())
            self.wfile.flush()
        rec["cut"], rec["chunks"] = False, 0
        try:
            ev("message_start", {"type": "message_start", "message": {"id": "msg_slow", "type": "message", "role": "assistant",
                                                                       "model": req.get("model"), "content": [], "stop_reason": None,
                                                                       "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}})
            ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for i in range(80):
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"w{i} "}})
                rec["chunks"] = i + 1
                time.sleep(0.5)
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": 80}})
            ev("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            rec["cut"] = True

    def log_message(self, *a):
        pass


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test removes Claude Code from that VM)")
        return 2
    td = tempfile.mkdtemp(prefix="mccvm-", dir="/tmp")
    procs, srv = [], None
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        run(td, procs, srv.server_address[1])
    finally:
        if srv:
            srv.shutdown()
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
            if isinstance(p, ts.Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if ts.results and all(ts.results) else 1


def run(td, procs, port):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []
    # This VM's settings plus the signed action log with text kept: the strictest place for a token to leak to.
    cfg = os.path.join(td, "test.env")
    with open(cfg, "w") as f:
        f.write(f'. "{os.environ["MCOMP_CONFIG"]}"\nACTION_LOG=on\nACTION_LOG_TEXT=keep\nACTION_LOG_KEY="{td}/keys/action-log"\n')
    events = os.path.join(td, "events")
    venv = dict(os.environ, MCOMP_CONFIG=cfg, MCOMP_VAULT_DIR=os.path.join(td, "v"), MCOMP_VAULT_BIN=BIN,
                MCOMP_VAULT_ALLOW_TEST_BUILD="1", MCOMP_VAULT_TEST_LOCAL_AUTH="allow", MCOMP_VAULT_TEST_PROMPT_TIMEOUT="60",
                MCOMP_VAULT_TEST_API_HTTP="1", MCOMP_EVENTS_DIR=events, MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"),
                MCOMP_KEEPER_POLL_S=os.environ.get("MCOMP_KEEPER_POLL_S", "5"))
    for k in ("MCOMP_VAULT_TEST_SSH", "MCOMP_VAULT_TEST_LISTENERS", "MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID"):
        venv.pop(k, None)
    outputs = []

    def tty(argv, answers, timeout=120):
        p = ts.Pty(argv, venv)
        try:
            for want, say in answers:
                p.expect(want, timeout=60)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        return rc, p.text()

    def mc(*args, timeout=600, stdin=None):
        r = subprocess.run([MC, *args], env=venv, capture_output=True, text=True, timeout=timeout,
                           input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        outputs.append((" ".join(args[:3]), r.stdout + r.stderr))
        return r

    def ops(cmd, stdin=""):
        """Raw SSH as the desktop user, for the test's own checks (not logged, never through metacomputer claude)."""
        return subprocess.run([MC, "ssh", "ops", cmd], env=venv, capture_output=True, text=True, timeout=180, input=stdin)

    def calls_since(n):
        return FakeAPI.got[n:]

    def msgs(n):
        return [g for g in calls_since(n) if g["method"] == "POST" and g["path"].split("?")[0] == "/v1/messages"]

    st = mc("status")
    check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-400:])
    for s in ("claude", "two", "three"):
        mc("claude", "stop", "--session", s, "--override")
    r = ops("pkill -x claude; rm -rf ~/.claude ~/.claude.json ~/.claude.json.backup ~/.local/bin/claude ~/.local/share/claude "
            "~/.local/state/claude ~/.cache/claude ~/claude-work ~/.local/state/metacomputer/claude; "
            "sed -i '/# metacomputer: Claude Code/d' ~/.bashrc; command -v claude || echo gone; command -v tmux")
    check("the VM starts without Claude Code (removed with its settings) and has tmux from the image",
          r.returncode == 0 and "gone" in r.stdout and "/usr/bin/tmux" in r.stdout, r.stdout + r.stderr)

    # --- the vault: a test build in its own folder, bound to this VM, the stand-in API as both secrets' host ---
    up = f"127.0.0.1:{port}"
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    rc2, out2 = tty([BIN, "set", "claude-sub", "--kind", "api", "--api-host", up],
                    [("Vault passphrase", PASS + "\n"), ("API key for claude-sub", DUMMY_SUB + "\n")])
    rc3, out3 = tty([BIN, "set", "claude-key", "--kind", "api", "--api-host", up, "--header", "x-api-key: {value}"],
                    [("Vault passphrase", PASS + "\n"), ("API key for claude-key", DUMMY_KEY + "\n")])
    rc4, out4 = tty([MC, "secrets", "bind"], [("Type yes to pin these settings", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("a test vault: a Bearer secret and an x-api-key secret for the stand-in API, bound to the VM (with its proxy account)",
          rc == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 and "Bound VM" in out4, (out2[-200:], out3[-200:], out4[-400:]))
    u = ts.Pty([BIN, "unlock", "--for", "30m", *allow], venv)
    procs.append(u)
    u.expect("Vault passphrase: ")
    u.send(PASS + "\n")
    u.expect("unlocked until")
    time.sleep(0.5)
    t_start = time.time()

    # --- start: install, first-run choices, tmux, a terminal on the desktop ---
    n0 = len(FakeAPI.got)
    r = mc("claude", "--token", "claude-sub", "--ttl", "15m", "--show", timeout=900)
    check("start: exit 0; Claude Code installed with the native installer and running in tmux",
          r.returncode == 0 and "Claude Code is running" in r.stdout, r.stdout[-1500:] + r.stderr[-1500:])
    screen = r.stdout.split("--- screen", 1)[-1]
    check("start: no first-run screen is waiting (theme, trust, login, API key)", not any(x in screen for x in FIRST_RUN),
          screen[-1500:])
    r = ops("~/.local/bin/claude --version; grep -c '# metacomputer: Claude Code' ~/.bashrc; bash -ic 'command -v claude'; "
            "python3 -c 'import json,os; d=json.load(open(os.path.expanduser(\"~/.claude.json\"))); "
            "print(d.get(\"hasCompletedOnboarding\"), "
            "d[\"projects\"][os.path.realpath(os.path.expanduser(\"~/claude-work\"))][\"hasTrustDialogAccepted\"])'")
    check("in the VM: claude in ~/.local/bin, which a terminal's shell finds (the PATH line in ~/.bashrc, once); "
          "onboarding done and the folder trusted (Claude Code keeps both)",
          "(Claude Code)" in r.stdout and "\n1\n" in r.stdout and "/.local/bin/claude" in r.stdout
          and "True True" in r.stdout, r.stdout + r.stderr)
    r = mc("sh", "-c", "wmctrl -l")
    check("--show: a terminal titled for the session is on the VM's desktop", "Claude in the metacomputer (claude)" in r.stdout,
          r.stdout)
    tok = ops("p=$(tmux -L metacomputer-claude display -p -t =claude: '#{pane_pid}'); "
              "tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p'").stdout.strip()
    check("Claude Code's environment holds the proxy token (mct_...) as CLAUDE_CODE_OAUTH_TOKEN", re.fullmatch(r"mct_[A-Za-z0-9_-]{20,}", tok)
          is not None, len(tok))
    r = ops("p=$(tmux -L metacomputer-claude display -p -t =claude: '#{pane_pid}'); "
            "tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(ANTHROPIC_BASE_URL|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC|ANTHROPIC_API_KEY)='")
    check("... with ANTHROPIC_BASE_URL at the proxy in the VM and nonessential traffic off; no ANTHROPIC_API_KEY",
          "ANTHROPIC_BASE_URL=http://127.0.0.1:18765/claude-sub\n" in r.stdout + "\n"
          and "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in r.stdout and "ANTHROPIC_API_KEY" not in r.stdout, r.stdout)

    def inner_claim():
        try:
            w = json.loads(mc("who", "--json").stdout or "{}")
        except ValueError:
            return {}
        return next((c for c in w.get("sessions") or [] if c.get("kind") == "inner" and c.get("session") == "claude"), {})

    # --- send / read / keys ---
    c0 = inner_claim()
    n1 = len(FakeAPI.got)
    r = mc("claude", "send", "Answer with the word PING only", "--wait", "--quiet", "4", "--timeout", "180")
    renewed = False
    for _ in range(30):
        if (inner_claim().get("last_call") or 0) > (c0.get("last_call") or 0):
            renewed = True
            break
        time.sleep(1)
    check("the keeper's own probes (a new `metacomputer claude _probe` each, fresh SSH credentials) see the screen change "
          "and renew its claim", bool(c0) and renewed, (c0, inner_claim()))
    m = msgs(n1)
    check("send --wait: the prompt went through the proxy with the dummy key in Authorization: Bearer (the oauth beta), "
          "and the answer is on the screen", r.returncode == 0 and f"PONG-{NONCE}" in r.stdout and m
          and all(x["auth"] == "bearer-sub" for x in m) and any("oauth" in x["beta"] for x in m)
          and any(x["last"] == "Answer with the word PING only" for x in m),
          (r.stdout[-1200:], r.stderr[-300:], m[-3:]))
    if os.environ.get("MCOMP_TEST_SHOT_DIR"):     # a look at the terminal on the desktop, kept for a person to see
        mc("shot", os.path.join(os.environ["MCOMP_TEST_SHOT_DIR"], "claude-inside.png"))
    other = [g for g in calls_since(n0) if g["path"].split("?")[0] not in ("/v1/messages", "/v1/messages/count_tokens")]
    print(f"      (other requests Claude Code made through the proxy: {[(g['method'], g['path']) for g in other][:8]})")
    n2 = len(FakeAPI.got)
    multi = "first line\nsecond line\nthird line: PING"
    r = mc("claude", "send", "-", "--wait", "--quiet", "4", "--timeout", "180", stdin=multi)
    m = msgs(n2)
    check("send - with three lines: one prompt, its newlines intact (a bracketed paste), answered",
          r.returncode == 0 and any(x["last"] == multi for x in m) and f"PONG-{NONCE} lines=3" in r.stdout,
          ([x["last"] for x in m], r.stdout[-800:]))
    n3 = len(FakeAPI.got)
    r = mc("claude", "send", "typed but held PING", "--no-enter")
    time.sleep(2)
    held = msgs(n3)
    r2 = mc("claude", "keys", "Enter")
    r3 = mc("claude", "wait", "--quiet", "4", "--timeout", "120")
    check("send --no-enter types without submitting; keys Enter submits; wait returns with the answer",
          r.returncode == 0 and not held and r2.returncode == 0 and r3.returncode == 0
          and any(x["last"] == "typed but held PING" for x in msgs(n3)) and f"PONG-{NONCE}" in r3.stdout,
          (held, r3.stdout[-600:]))
    r = mc("claude", "read", "--lines", "60")
    check("read: the conversation, escaped (no raw escape characters)", r.returncode == 0 and f"PONG-{NONCE}" in r.stdout
          and "\x1b" not in r.stdout, r.stdout[-600:])
    r = mc("claude", "status", "--json")
    d = json.loads(r.stdout or "{}")
    check("status: running, a terminal attached, the token live", r.returncode == 0 and d.get("state") == "running"
          and d.get("viewers") not in (None, "0") and (d.get("token") or {}).get("live") is True, r.stdout)
    w = mc("who")
    check("its keeper holds the computer while it works: status says so, and `metacomputer who` names it",
          (d.get("keeper") or {}).get("running") is True and (d.get("keeper") or {}).get("active") is True
          and "Claude Code inside the VM (metacomputer claude, session claude)" in w.stdout, (d.get("keeper"), w.stdout[-500:]))

    # --- renewing its token: the same token lives longer, the same Claude Code keeps going ---
    r = mc("claude", "renew", "--ttl", "20m")
    st = json.loads(mc("claude", "status", "--json").stdout or "{}").get("token") or {}
    check("renew: the session's token now lives about 20 minutes from now (it had less), recorded on the Mac",
          r.returncode == 0 and "now lives until" in r.stdout and st.get("live") is True
          and 1100 <= int(st.get("seconds_left") or 0) <= 1200 and st.get("expires") and st["expires"] in r.stdout,
          (r.stdout + r.stderr, st))
    n5 = len(FakeAPI.got)
    r = mc("claude", "send", "still there? PING", "--wait", "--quiet", "4", "--timeout", "180")
    same = ops("p=$(tmux -L metacomputer-claude display -p -t =claude: '#{pane_pid}'); "
               "tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p'").stdout.strip()
    check("... and the same Claude Code, with the same token, keeps answering through the proxy",
          r.returncode == 0 and f"PONG-{NONCE}" in r.stdout and same == tok
          and any(x["last"] == "still there? PING" and x["auth"] == "bearer-sub" for x in msgs(n5)), r.stdout[-600:])

    # --- the keeper outlives its first credentials (run under SSH_AUTH=cert with a short SSH_CERT_TTL) ---
    span = int(os.environ.get("MCOMP_TEST_KEEPER_SPAN_S") or 0)
    if span:
        t_end, k = time.time() + span, 0
        while time.time() < t_end:
            k += 1
            mc("claude", "send", f"PING {k}", "--wait", "--quiet", "3", "--timeout", "60")
            time.sleep(15)
        mc("claude", "send", "PING final", "--wait", "--quiet", "3", "--timeout", "60")
        poll = int(venv["MCOMP_KEEPER_POLL_S"])
        renewed_lately = False
        for _ in range(6 * poll):
            c = inner_claim()
            if c and time.time() - float(c.get("last_call") or 0) < 3 * poll + 5:
                renewed_lately = True
                break
            time.sleep(1)
        check(f"after {span} s (past its first SSH credentials' lifetime), the keeper still probes, renews and holds "
              "the computer", renewed_lately and bool(inner_claim().get("active")), inner_claim())

    # --- an API key (x-api-key), a model, --skip-permissions ---
    n4 = len(FakeAPI.got)
    r = mc("claude", "--session", "two", "--token", "claude-key", "--model", "sonnet", "--skip-permissions", timeout=300)
    screen = r.stdout.split("--- screen", 1)[-1]
    check("start with an x-api-key secret and --skip-permissions: running, no API-key or bypass-permissions question",
          r.returncode == 0 and not any(x in screen for x in FIRST_RUN), r.stdout[-1500:] + r.stderr[-500:])
    r = mc("claude", "send", "--session", "two", "PING please", "--wait", "--quiet", "4", "--timeout", "180")
    m = msgs(n4)
    check("... its prompt went through the proxy with the dummy key in x-api-key, answered",
          r.returncode == 0 and f"PONG-{NONCE}" in r.stdout and m and all(x["auth"] == "x-api-key" for x in m)
          and any(x["last"] == "PING please" for x in m), (r.stdout[-800:], m[-3:]))
    r = ops("p=$(tmux -L metacomputer-claude display -p -t =two: '#{pane_pid}'); tr '\\0' ' ' < /proc/$p/cmdline; echo; "
            "tr '\\0' '\\n' < /proc/$p/environ | grep -cE '^(ANTHROPIC_API_KEY=mct_|CLAUDE_CODE_OAUTH_TOKEN=)'")
    check("... as ANTHROPIC_API_KEY (no CLAUDE_CODE_OAUTH_TOKEN), with --model sonnet and --dangerously-skip-permissions",
          "--model sonnet" in r.stdout and "--dangerously-skip-permissions" in r.stdout and r.stdout.strip().endswith("1"),
          r.stdout)

    # --- nothing leaked ---
    # The strings searched for travel on stdin into a file in /dev/shm (outside the folders searched), so no
    # grep's own command line holds them.
    find = ("f=$(mktemp -p /dev/shm); cat > \"$f\"; grep -rlsFf \"$f\" ~ /tmp /var/tmp 2>/dev/null; "
            "for p in /proc/[0-9]*; do {what}; done | grep -cFf \"$f\"; rm -f \"$f\"")
    r = ops(find.format(what="tr '\\0' '\\n' < $p/environ 2>/dev/null; echo; tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo"),
            stdin=DUMMY_SUB + "\n" + DUMMY_KEY + "\n")
    check("neither dummy key is anywhere in the VM: files in ~, /tmp, /var/tmp, any process's environment or command line",
          r.stdout.split() == ["0"], r.stdout + r.stderr)
    ps = subprocess.run(["ps", "-axww", "-o", "args"], capture_output=True, text=True).stdout
    check("the token is on no command line on the Mac", tok and tok not in ps)
    r = ops(find.format(what="tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo"), stdin=tok + "\n")
    check("the token is in no file in the VM (the launcher deleted its file) and on no command line there "
          "(only in Claude Code's environment)", r.stdout.split() == ["0"], r.stdout)
    cfg_dir = os.path.dirname(os.environ["MCOMP_CONFIG"])
    found = []
    for root in (td, os.path.join(os.path.dirname(cfg_dir), os.path.basename(cfg_dir)),):
        for d, _, files in os.walk(root):
            for n in files:
                p = os.path.join(d, n)
                try:
                    if os.path.getmtime(p) >= t_start - 5 and tok.encode() in open(p, "rb").read():
                        found.append(p)
                except OSError:
                    pass
    check("the token is in no file on the Mac written during the test (signed action log with text kept, events, the vault's "
          "audit log, the session record, VM logs)", tok and not found, found)
    check("the token and the dummy keys are in no output of a metacomputer command",
          not any(tok in o or DUMMY_SUB in o or DUMMY_KEY in o for _, o in outputs), [w for w, o in outputs if tok in o])

    # --- a token that ends mid-answer cuts the answer (the vault proxy checks the token while it streams) ---
    tid2 = (json.loads(mc("claude", "status", "--session", "two", "--json").stdout or "{}").get("token") or {}).get("id")
    n6 = len(FakeAPI.got)
    mc("claude", "send", "--session", "two", "SLOWSTREAM please")
    slow = None
    for _ in range(40):
        slow = next((g for g in FakeAPI.got[n6:] if "SLOWSTREAM" in g.get("last", "") and "chunks" in g), None)
        if slow and slow["chunks"] >= 4:
            break
        time.sleep(0.5)
    rv = mc("secrets", "revoke", tid2 or "00000000")
    for _ in range(30):
        if slow and slow.get("cut"):
            break
        time.sleep(0.5)
    check("revoking the token mid-answer cuts the answer: the proxy hangs up on the stand-in API within seconds",
          bool(slow) and rv.returncode == 0 and slow.get("cut") is True and slow.get("chunks", 80) < 40,
          (slow, rv.stdout[-200:]))

    # --- stop, revoke ---
    r = mc("claude", "stop", "--session", "two", "--revoke")
    check("stop --revoke: the session ends and its token is revoked", r.returncode == 0 and "revoked" in r.stdout, r.stdout + r.stderr)
    code_before = ops("read -r t; curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $t\" "
                      "-H 'content-type: application/json' -d '{}' http://127.0.0.1:18765/claude-sub/v1/messages/count_tokens",
                      stdin=tok + "\n").stdout.strip()
    r = mc("claude", "stop")
    tid = re.search(r"metacomputer secrets revoke ([0-9a-f]{8})", r.stdout)
    check("stop without --revoke: ends the session, prints the token's id and the command to end it",
          r.returncode == 0 and tid is not None, r.stdout + r.stderr)
    r2 = mc("secrets", "revoke", tid.group(1) if tid else "00000000")
    code_after = ops("read -r t; curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $t\" "
                     "-H 'content-type: application/json' -d '{}' http://127.0.0.1:18765/claude-sub/v1/messages/count_tokens",
                     stdin=tok + "\n").stdout.strip()
    check("the proxy took the token before the revoke (200) and refuses it after (401)", code_before == "200" and code_after == "401"
          and r2.returncode == 0, (code_before, code_after))
    time.sleep(1)
    r = mc("sh", "-c", "tmux -L metacomputer-claude ls 2>&1; wmctrl -l >/dev/null && wmctrl -l | grep -c 'Claude in the metacomputer'")
    check("no session left; the desktop terminal closed with it", "no server running" in r.stdout
          and r.stdout.strip().endswith("0"), r.stdout + r.stderr)
    r = mc("claude", "read")
    check("read with no session: exit 6", r.returncode == 6, r.stderr)
    w = mc("who")
    check("after stop, no keeper holds the computer", "Claude Code inside the VM" not in w.stdout, w.stdout[-400:])

    u.send("\x03")
    u.wait(15)


if __name__ == "__main__":
    sys.exit(main())
