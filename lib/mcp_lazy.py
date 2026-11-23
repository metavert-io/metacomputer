#!/usr/bin/env python3
"""On-demand MCP bridge to the virtual computer (`metacomputer mcp-lazy`).

Claude Code registers this for every project (user scope). It starts instantly
whether or not the VM is running, so the `metacomputer` tools exist in every
session without a restart:

- initialize and tools/list (and the resources/prompts lists) are answered from
  a cached copy of Cua Driver's own handshake for the pinned driver version
  (config/driver-mcp-cache.json, refreshed in .run/ on every live connection),
  with an identity note in front of the driver's instructions;
- the first call that needs the driver connects to this VM's hub (lib/mcp_hub.py,
  started if it isn't running) and from then on messages pass through it. The
  hub holds the one connection to the VM and applies every guard: one controller
  at a time, the Chrome guard, bounded mode, the coordinate guard, the action
  log and the event stream;
- while the VM is off, a call returns a clear "start it, then retry" error;
- a dropped connection (the VM or the hub stopped) is reopened on the next call.

Two tools of its own, answered here, never by the driver: `list_computers` (the user's computers, as `metacomputer
list --json` has them, and which one this session's tools act on) and `select_computer` (this session's tools act on
another computer from then on, through that computer's own hub: one hub per computer). The computer they act on, from
the start and after a selection, is recorded for the agent session above (lib/sessionvm.py), so the session's commands
that name no computer stay on it (bin/metacomputer's session guard).

With MCP_HUB=off the same guards run in this process instead, on a connection of
its own (the hub's core, lib/mcp_hub.py, with this session as its only client).

  mcp_lazy.py                      run as the stdio MCP server
  mcp_lazy.py --refresh-cache [--seed]   connect once and save the handshake
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_hub  # noqa: E402
import mcomp_sdk  # noqa: E402
import owners  # noqa: E402
import sessionvm  # noqa: E402
import wirelimits  # noqa: E402

LAB = mcp_hub.LAB
CLI = mcp_hub.CLI
IDENTITY, OFF_MSG, DROPPED_MSG = mcp_hub.IDENTITY, mcp_hub.OFF_MSG, mcp_hub.DROPPED_MSG
NO_HUB = ("The metacomputer bridge couldn't reach this VM's hub ({}). Check `" + CLI + " status` and `" + CLI
          + " hub status`, then call again. (This is the virtual computer, an Ubuntu VM, not the user's Mac.)")
WATCH_PARENT_S = 5
SHUTDOWN_POLL_S = 0.2               # how often the main loop looks whether the shutdown was asked for
ENDING = "The metacomputer MCP server is ending; nothing was done. (A new session starts it again.)"
CLI_TIMEOUT_S = 60
# What picks a computer, in the environment: never carried from one computer to another (select_computer).
CHOICE_KEYS = ("MCOMP_VM", "MCOMP_CONFIG", "MCOMP_NO_DEFAULT_VM")
COMPUTER_TOOLS = [
    {"name": "list_computers",
     "description": ("metacomputer: the user's computers (the virtual computers this Mac's metacomputer has): each one's "
                     "name, where it is, its state, which is the default computer, and which one this session's "
                     "metacomputer tools act on. Changes nothing; works while they're off."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"title": "List computers", "readOnlyHint": True, "openWorldHint": False}},
    {"name": "select_computer",
     "description": ("metacomputer: make the computer `name` (from list_computers) the one this session's metacomputer "
                     "tools act on, until the session ends or another select_computer. Starts, stops and changes "
                     "nothing: a computer that's off says so on the next call. Shell commands for it then name it "
                     "(`metacomputer --vm NAME ...`) unless it's the default computer; the result says which."),
     "inputSchema": {"type": "object", "properties": {"name": {
         "type": "string", "description": "The computer's name, as list_computers shows it",
         "pattern": "^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$"}}, "required": ["name"], "additionalProperties": False},
     "annotations": {"title": "Select computer", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False}},
]
COMPUTER_TOOL_NAMES = {t["name"] for t in COMPUTER_TOOLS}


def same_computer(a, b):
    """Whether two computers (sessionvm.computer_of) are one: the same VM name, SSH port and folder."""
    return all(a.get(k) == b.get(k) for k in ("vm", "ssh_port", "run_dir"))


def with_computer_tools(tools):
    """The driver's tools and the bridge's own two (a driver tool of the same name is left out)."""
    return [t for t in tools or [] if t.get("name") not in COMPUTER_TOOL_NAMES] + json.loads(json.dumps(COMPUTER_TOOLS))


class Front:
    def __init__(self, env=None, out=None):
        self.env = dict(os.environ if env is None else env)
        self.out = out or sys.stdout
        self.out_lock = threading.Lock()
        self.conn_lock = threading.Lock()
        self.conn = None
        self.outstanding = set()       # ids of requests passed to the hub, still unanswered
        self.olock = threading.Lock()
        self.cache = mcp_hub.load_cache(mcp_hub.paths(self.env)["cache"]) or {}
        self.served = None             # names-hash of the tools this session was given from the cache
        self.pid = os.getpid()
        self.stopping = False
        self.start_env = dict(self.env)    # the computer this bridge started on (select_computer can come back to it)
        self.selected = False          # True once select_computer chose the computer
        self.agent = self.agent_name = None   # the agent session above this bridge (register)
        self.list_ids = set()          # tools/list requests passed to the hub: its answer gets COMPUTER_TOOLS
        self.retiring = []             # threads ending the connections of computers before a select_computer
        # Letting go of a connection (select_computer's switch, a refusal's close) and the shutdown don't overlap: the
        # shutdown, which only the main loop makes (finish), waits until the connection being let go of is one it
        # waits for, and none starts after it. Signal handlers only set stop_requested.
        self.switch_cv = threading.Condition()
        self.switching = False
        self.stop_requested = False
        # Hubs from an earlier metacomputer (no conn_claims) found serving a computer: (vm, ssh port, run folder) ->
        # (hub pid, its start time). Switching away from that computer is refused while that same hub runs.
        self.legacy_hubs = {}

    # --- output ------------------------------------------------------------------------
    def send(self, msg):
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        with self.out_lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def reply(self, msg, result):
        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def tool_error(self, msg, text):
        self.reply(msg, {"content": [{"type": "text", "text": text}], "isError": True})

    # --- the hub -------------------------------------------------------------------------
    def connected(self):
        c = self.conn
        return c is not None and c.alive

    def connect(self):
        """None when connected to the hub (or this process's own core), else a message for the caller. Once the server
        is ending, nothing new is opened: shutdown closes what's there, and takes it under this same lock."""
        with self.conn_lock:
            if self.stopping:
                return ENDING
            if self.connected():
                return None
            try:
                conn = mcomp_sdk.open_connection(self.env, kind="claude-code", tools_hash=self.served)
            except mcp_hub.HubError as e:
                return NO_HUB.format(e)
            except OSError as e:
                return NO_HUB.format(e)
            conn.on_message = self.from_hub
            conn.on_close = lambda c=conn: self.hub_closed(c)
            self.conn = conn
            conn.start()
            return None

    def from_hub(self, msg):
        if "method" not in msg:
            rid = json.dumps(msg.get("id"))
            with self.olock:
                self.outstanding.discard(rid)
                listing = rid in self.list_ids
                self.list_ids.discard(rid)
            if listing and isinstance(msg.get("result"), dict) and isinstance(msg["result"].get("tools"), list):
                msg = {**msg, "result": {**msg["result"], "tools": with_computer_tools(msg["result"]["tools"])}}
        self.send(msg)

    def hub_closed(self, conn):
        if getattr(conn, "retired", False):
            return                       # the computer before a select_computer: nothing of this session's waits on it
        if self.conn is conn:
            self.conn = None
        if self.stopping:
            return
        with self.olock:
            ids, self.outstanding = list(self.outstanding), set()
        for key in ids:
            self.send({"jsonrpc": "2.0", "id": json.loads(key), "error": {"code": -32603, "message": DROPPED_MSG}})

    def forward(self, msg):
        if msg.get("id") is not None:
            with self.olock:
                self.outstanding.add(json.dumps(msg["id"]))
        c = self.conn
        if c is None or not c.send(msg):
            if msg.get("id") is not None:
                with self.olock:
                    self.outstanding.discard(json.dumps(msg["id"]))
                if msg.get("method") == "tools/call":
                    self.tool_error(msg, DROPPED_MSG)
                else:
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": DROPPED_MSG}})

    # --- messages from Claude Code --------------------------------------------------------
    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method is None:                       # an answer to a server request: the hub never sends those
            return
        if method == "initialize":
            want = (msg.get("params") or {}).get("protocolVersion")
            if not self.cache.get("initialize") and self.connect() is None:
                self.forward(msg)
                return
            self.reply(msg, mcp_hub.initialize_result(self.cache, want))
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            self.reply(msg, {})
        elif method == "tools/list" and not self.connected():
            tools = (self.cache.get("tools") or {}).get("tools") or []
            self.served = mcp_hub.tool_names_hash(tools)       # the driver's tools: what the hub compares
            self.reply(msg, {"tools": with_computer_tools(tools)})
        elif method == "tools/list" and mid is not None:
            with self.olock:
                self.list_ids.add(json.dumps(mid))
            self.forward(msg)
        elif method == "tools/call" and mid is not None and (msg.get("params") or {}).get("name") in COMPUTER_TOOL_NAMES:
            p = msg.get("params") or {}
            args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
            if p["name"] == "list_computers":
                self.list_computers(msg)
            else:
                self.select_computer(msg, args)
        elif method in ("resources/list", "resources/templates/list", "prompts/list") and not self.connected():
            key = method.split("/")[0]
            cached = self.cache.get(key) if method != "resources/templates/list" else None
            empty = {"resources": []} if method == "resources/list" else (
                {"resourceTemplates": []} if method == "resources/templates/list" else {"prompts": []})
            self.reply(msg, cached or empty)
        elif method == "logging/setLevel":
            self.reply(msg, {})
        elif mid is None:                        # notifications (cancelled, ...)
            if self.connected():
                self.forward(msg)
        else:                                    # tools/call and anything else: through the hub
            err = self.connect()
            if err:
                if method == "tools/call":
                    self.tool_error(msg, err)
                else:
                    self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": err}})
                return
            self.forward(msg)

    # --- which computer: list_computers, select_computer ------------------------------------------------------
    def computer(self):
        return sessionvm.computer_of(self.env)

    def register(self):
        """The agent session above this bridge (Claude Code or Codex), and a record that this session's tools act on
        this bridge's computer (lib/sessionvm.py). Nothing without an agent session above it. Never fails."""
        try:
            table = owners.ps_table()
            self.agent = owners.claude_of(self.pid, table)
            self.agent_name = owners.agent_of(self.agent, table) if self.agent else None
        except Exception:
            self.agent = None
        self.note_computer()

    def note_computer(self):
        if self.agent:
            sessionvm.note_bridge(self.agent, self.pid, self.computer(), self.selected, self.agent_name)

    def base_env(self):
        """The environment this bridge was started with, without any computer's settings or choice: the variables
        bin/metacomputer added (it names the ones that were there, MCOMP_BRIDGE_KEYS) and CHOICE_KEYS left out."""
        names = self.start_env.get("MCOMP_BRIDGE_KEYS")
        keep = set(names.split()) if isinstance(names, str) else None
        env = {k: v for k, v in self.start_env.items() if not k.startswith("MCOMP_") or keep is None or k in keep}
        for k in CHOICE_KEYS:
            env.pop(k, None)
        return env

    def run_cli(self, args, env):
        """A metacomputer command of this checkout's, its output read; (exit status, stdout, stderr)."""
        try:
            r = subprocess.run([mcp_hub.MCOMP_CLI, *args], env=env, capture_output=True, text=True,
                               timeout=CLI_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=LAB)
            return r.returncode, r.stdout, r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            return 1, "", str(e)

    @staticmethod
    def said(text, n=600):
        """A command's message, for a result: plain characters, the last n of them."""
        return mcp_hub.clean(" ".join((text or "").split()), 100000)[-n:]

    def reply_tool(self, msg, text, structured):
        """A result with its words in both parts: Claude Code shows the model the structured one."""
        self.reply(msg, {"content": [{"type": "text", "text": text}], "structuredContent": structured})

    def list_computers(self, msg):
        rc, out, err = self.run_cli(["list", "--json"], self.base_env())
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("vms"), list):
            self.tool_error(msg, f"`{CLI} list` didn't answer (exit {rc}): {self.said(err) or 'no output'}")
            return
        cur = self.computer() or {}
        rows = []
        for v in data["vms"]:
            if not isinstance(v, dict) or not isinstance(v.get("name"), str):
                continue
            row = {"name": v["name"], "place": v.get("place"), "where": v.get("where"), "state": v.get("state"),
                   "default": bool(v.get("default")),
                   "this_session": v["name"] == cur.get("vm") and str(v.get("ssh_port") or "") == cur.get("ssh_port"),
                   "settings": v.get("settings")}
            if v.get("error"):
                row["error"] = self.said(v["error"], 300)
            rows.append(row)
        if cur and not any(r["this_session"] for r in rows):
            rows.append({"name": cur["vm"], "place": "mac", "where": "this Mac", "state": "unknown", "default": False,
                         "this_session": True, "settings": cur["config"] or None})
        problems = [self.said(p, 300) for p in data.get("problems") or [] if isinstance(p, str)]
        result = {"computers": rows, "default": data.get("default"), "this_session": cur.get("vm"),
                  "selected": self.selected, "problems": problems}
        lines = [f"metacomputer: {len(rows)} computer(s); this session's tools act on {cur.get('vm') or '?'}"
                 + (" (selected)." if self.selected else ".")]
        for r in rows:
            tags = [t for t, on in (("the default", r["default"]), ("this session's", r["this_session"])) if on]
            lines.append(f"- {r['name']}: {r.get('where') or '?'}, {r.get('state') or '?'}"
                         + (f"; {', '.join(tags)}" if tags else "") + (f"; settings refused: {r['error']}" if r.get("error") else ""))
        lines += [f"! {p}" for p in problems]
        lines.append(f"select_computer switches this session's tools. In the shell, name a computer that isn't the "
                     f"default: {CLI} --vm NAME ...")
        result["message"] = lines[0] + " " + lines[-1]
        self.reply_tool(msg, "\n".join(lines), result)

    def select_computer(self, msg, args):
        name = args.get("name")
        if not isinstance(name, str) or not sessionvm.NAME_RE.fullmatch(name):
            self.tool_error(msg, "select_computer needs a computer's name (letters, digits, '.', '-' and '_'), as "
                                 "list_computers shows it.")
            return
        cur = self.computer() or {}
        with self.olock:
            busy = len(self.outstanding)
        if busy and name != cur.get("vm"):
            self.tool_error(msg, f"{busy} call(s) on {cur.get('vm')} haven't answered yet; select another computer once "
                                 "they have. Nothing was changed.")
            return
        start = sessionvm.computer_of(self.start_env) or {}
        if name == cur.get("vm"):
            env = self.env
        elif name == start.get("vm"):
            env = dict(self.start_env)
        else:
            rc, out, err = self.run_cli(["--vm", name, "hub", "env", "--json"], self.base_env())
            try:
                found = json.loads(out) if rc == 0 else None
            except ValueError:
                found = None
            if not isinstance(found, dict) or found.get("MCOMP_VM_NAME") != name \
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in found.items()):
                self.tool_error(msg, f"{name} can't be selected: {self.said(err) or 'its settings did not load'} "
                                     f"(list_computers shows the computers). This session's tools still act on "
                                     f"{cur.get('vm')}.")
                return
            env = self.base_env()
            env.update(found)
            if mcp_hub.mode(env) == "off":
                # The in-process core keeps its claims where this process's owners module looks (the computer it
                # started on), so another computer needs its own hub.
                self.tool_error(msg, f"{name} has MCP_HUB=off, so this session can't reach it from here: its tools "
                                     f"need that computer's own hub. Start a new session on it instead. This session's "
                                     f"tools still act on {cur.get('vm')}.")
                return
        if env is not self.env:
            if not self.begin_switch():
                self.tool_error(msg, "The metacomputer MCP server is ending; nothing was switched.")
                return
            try:
                refused = self.older_hub_refusal(cur)
                if refused is None:
                    self.switch_to(env)
            finally:
                self.end_switch()
            if refused is not None:
                self.tool_error(msg, refused)
                return
        self.selected = True
        self.note_computer()
        new = self.computer() or {"vm": name}
        # The command to use for it: bare only when a command that names no computer reaches this same one (its name,
        # SSH port and folder), else one that names it as its settings do (--vm, or its own MCOMP_CONFIG file).
        bare = self.bare_computer()
        is_default = bool(bare) and same_computer(bare, new)
        prefix = CLI if is_default else (sessionvm.naming(new) if new.get("lab") else f"{CLI} --vm {name}")
        default = bare.get("vm") if bare else None
        result = {"computer": name, "previous": cur.get("vm"), "default": default, "is_default": is_default,
                  "where": "this Mac", "place": "mac", "cli": prefix, "settings": new.get("config") or None}
        text = (f"metacomputer: this session's tools now act on {name} (this Mac)"
                + (f", not {cur.get('vm')}" if cur.get("vm") and cur.get("vm") != name else "") + ". "
                + ("It's the default computer, so shell commands reach it as they are: " if is_default else
                   "Name it in every shell command for it: ")
                + f"`{prefix} COMMAND`. Nothing was started or stopped; if it's off, a call says so.")
        result["message"] = text
        self.reply_tool(msg, text, result)

    def retire(self, old):
        """Let go of the computer before a select_computer: its hub lets go of this session (its claim, its driver
        session) when the connection closes; with MCP_HUB=off the core ends its own connection once no handoff is
        open (bounded). In a thread of its own, which shutdown waits for. Called only by switch_to, between
        begin_switch and end_switch, so a shutdown never starts while one is being started or listed."""
        old.retired = True
        t = threading.Thread(target=old.close, kwargs={"keep_for_handoff": True}, daemon=True, name="mcp-lazy-retire")
        t.start()
        self.retiring.append(t)

    def switch_to(self, env):
        """This session's tools act on the computer env names from now on; the connection to the one before is let go
        of (retire) first."""
        with self.conn_lock:
            old, self.conn = self.conn, None
            self.env = env
            with self.olock:
                self.list_ids.clear()
        if old is not None:
            self.retire(old)
        self.cache = mcp_hub.load_cache(mcp_hub.paths(env)["cache"]) or self.cache

    def begin_switch(self):
        """Before anything that lets go of a connection (a switch, a refusal's close). False when the server is
        ending: nothing is let go of then (the shutdown closes it, and waits for it)."""
        with self.switch_cv:
            if self.stopping:
                return False
            self.switching = True
            return True

    def end_switch(self):
        with self.switch_cv:
            self.switching = False
            self.switch_cv.notify_all()

    def on_signal(self, *_):
        """SIGTERM, SIGHUP: only ask for the shutdown (a flag). It runs on the main thread's own loop (serve), never in
        the handler, which may have interrupted anything, a thread's start included."""
        self.stop_requested = True

    def finish(self):
        """The shutdown the main loop makes once it's asked for: after a switch or a refusal that's letting go of a
        connection is done (so that connection is one it waits for), and with no new one starting."""
        with self.switch_cv:
            self.stopping = True
            while self.switching:
                self.switch_cv.wait()
        self.shutdown()

    def older_hub_refusal(self, cur):
        """Why switching away from cur is refused when its hub is from an earlier metacomputer (its hello doesn't say
        conn_claims), else None. That hub drops a claim by process when a connection goes, so a disconnect it handles
        late could take a later connection's claim. So nothing changes: this session's connection to it stays as it is
        (the hub sees no disconnect), and the hub is remembered (pid and start time), so every switch away from cur is
        refused until that process is gone. Between begin_switch and end_switch."""
        conn = self.conn
        info = getattr(conn, "info", None) if conn is not None else None
        k = (cur.get("vm"), cur.get("ssh_port"), cur.get("run_dir"))
        if isinstance(info, dict) and "conn_claims" not in (info.get("features") or []):
            pid = info.get("hub_pid")
            self.legacy_hubs[k] = (pid, sessionvm.proc_start(pid) if isinstance(pid, int) else None)
        elif self.legacy_hub(cur) is None:
            return None
        pid = self.legacy_hubs[k][0]
        return (f"{cur.get('vm')}'s hub (pid {pid}) is from an earlier metacomputer, which can't tell this session's "
                "connections apart, so switching away and back could drop this session's claim on it. Nothing was "
                "switched or changed (this session's tools still act on it), and switching away from it stays refused "
                "while that hub runs. To switch, that hub has to be replaced: ask the user to stop it when nothing else "
                f"needs it (`{sessionvm.naming(cur) if cur.get('lab') else CLI} hub stop`, which disconnects this "
                "session too; its next call starts a current hub), then select again.")

    def legacy_hub(self, comp):
        """The hub from an earlier metacomputer (no conn_claims) this session found serving comp, while that same
        process (pid and start time) still runs, else None."""
        k = (comp.get("vm"), comp.get("ssh_port"), comp.get("run_dir"))
        found = self.legacy_hubs.get(k)
        if found:
            pid, start = found
            # Its start time, or when that couldn't be read, whether this computer's hub record still names it.
            if (sessionvm.proc_start(pid) == start) if start else mcp_hub.hub_pid(self.env) == pid:
                return pid
        self.legacy_hubs.pop(k, None)
        return None

    def bare_computer(self):
        """The computer a command that names none acts on from this checkout (`metacomputer hub env --json` with no
        computer chosen), or None when that can't be told (the command refuses, say)."""
        rc, out, _ = self.run_cli(["hub", "env", "--json"], self.base_env())
        try:
            found = json.loads(out) if rc == 0 else None
        except ValueError:
            return None
        return sessionvm.computer_of(found) if isinstance(found, dict) else None

    def shutdown(self, *_):
        """End the bridge: no connection opens after this (connect looks at stopping under conn_lock, where the current
        one is taken), the current one is closed, then the ones a select_computer let go of are waited for."""
        self.stopping = True
        sessionvm.drop_bridge(self.agent, self.pid)
        with self.conn_lock:
            c, self.conn = self.conn, None
        if c is not None:
            # The hub drops this session's claim and ends its driver session; with MCP_HUB=off, while the user has the
            # computer (a handoff), this bridge's own connection ends only once it has ended (bounded).
            c.close(keep_for_handoff=True)
        for t in list(self.retiring):
            t.join()                     # the computers before a select_computer, the same way (each bounded)
        os._exit(0)

    def watch_parent(self):
        """Claude Code went away without closing our stdin: don't linger (and keep the computer claimed)."""
        while True:
            time.sleep(WATCH_PARENT_S)
            if os.getppid() == 1:
                self.stop_requested = True
                return

    def read_stdin(self, stdin):
        """Claude Code's messages, handled on this thread of their own; the end of input asks for the shutdown. Once the
        server is ending, a request gets an error and nothing else is done for it."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = wirelimits.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if self.stopping:
                if msg.get("id") is not None and msg.get("method") == "tools/call":
                    self.tool_error(msg, ENDING)
                elif msg.get("id") is not None and msg.get("method"):
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": ENDING}})
                continue
            try:
                self.handle(msg)
            except Exception as e:  # never let one message kill the bridge
                if msg.get("id") is not None and msg.get("method"):
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})
        self.stop_requested = True

    def serve(self, stdin=None):
        """Messages are handled on a thread of their own (read_stdin); the main thread only waits for the shutdown to be
        asked for (a signal, the end of input, the agent gone) and makes it (finish), outside any signal handler."""
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGHUP, self.on_signal)
        self.register()
        threading.Thread(target=self.watch_parent, daemon=True, name="mcp-lazy-parent").start()
        threading.Thread(target=self.read_stdin, args=(stdin or sys.stdin,), daemon=True, name="mcp-lazy-stdin").start()
        while not self.stop_requested:
            time.sleep(SHUTDOWN_POLL_S)
        self.finish()

if __name__ == "__main__":
    if "--refresh-cache" in sys.argv:
        sys.exit(mcp_hub.main(["mcp_hub.py", "refresh-cache"] + (["--seed"] if "--seed" in sys.argv else [])))
    if os.environ.get("MCOMP_HUB_PID"):
        # The hub's own driver connection must never come back here (a loop through the hub).
        print("metacomputer: mcp-lazy can't run inside the hub's own connection", file=sys.stderr)
        sys.exit(1)
    Front().serve()
