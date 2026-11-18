#!/usr/bin/env python3
"""Two MCP tools the hub serves itself, shaped like the ones Cua Spaces' Keyvault documents, so an agent
written for Spaces finds them here: `request_site_login` (sign in to a site in the VM's page-mode Chrome
with a login saved in `metacomputer secrets`) and `teleport_browser_session` (load a site's sealed
session, imported from the user's own Chrome, into the VM's Chrome). Nothing here is shared with Spaces;
the vault behind them is metacomputer's own (SECURITY.md, "Secrets and logins").

Both go through every guard of the hub (lib/mcp_hub.py calls them after its one-controller check, as for
any acting tool): the driver calls they make (browser_prepare, get_browser_state, browser_navigate,
browser_click) are the hub's own calls on behalf of that client, so the action log names the client and
bounded mode's manifest applies to them as to any call. The secrets themselves never pass through here:
the vault types the values through its own fill path (`metacomputer secrets login|fill|session load`) with
its own approvals, and this module gets back status, a site and a masked username, never a value.

Keyvault's two-step shape is kept: the first call starts the use and answers `pending` with a request_id
when it waits for the user; a later call with that request_id collects the outcome (waiting up to
wait_secs). Here the use goes ahead as soon as the user approves, whether or not the agent calls again.

The vault sees these requests as the hub's own (no Claude Code session, no project): the hub serves every
session from one process, so the client's pid it passes on (`--on-behalf-pid`) is shown and recorded as an
unverified claim and never satisfies a project limit, a rule or a grant. What the hub checked is bound into
the vault's call instead: the login's exact origin (`--origin`), the Chrome it read (`--browser-pid`: the one it
opened, or the one a tab passed in belongs to, checked through the driver; the vault refuses a hub request naming
no Chrome), the sites of a session
load (`--site`). After a login nothing the page chose comes back: not its URL, not even its host (a page, or the VM,
can spell a value in a host name), only whether it's still on the login's own origin (`same_site`).
"""
import json
import os
import re
import secrets as _random
import subprocess
import threading
import time

LIB = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(LIB)
CLI = os.path.join(LAB, "bin", "metacomputer")
FIRST_WAIT_S = 3.0          # a use that needs no approval usually finishes within this, on the first call
JOB_TTL_S = 600             # a request's outcome is kept this long for the agent to collect

TOOLS = [
    {
        "name": "request_site_login",
        "description": (
            "Sign in to a website in the virtual computer's page-mode Chrome with a login the user saved in "
            "`metacomputer secrets` (imported from their browser or a password manager), without the password ever "
            "reaching you. The vault types the username and password into the page's fields through its own fill "
            "path, only on the saved login's exact site, after the user approves (Touch ID, their login password, the "
            "vault passphrase, or their unlock terminal; identity providers always ask). The first call answers "
            "status \"pending\" with a request_id while the user decides; call again with the same url and request_id "
            "to get \"filled\" (site, origin, a masked username_hint, submitted, same_site (whether the page is still on "
            "the login's origin; no address of the page is returned) and the tab) or \"denied\". "
            "Pass the tab you opened (session, target_id, tab_id from get_browser_state, and pid, the Chrome process you "
            "bound it with; the tool checks the tab is in that Chrome) to sign in there; without one it opens page-mode "
            "Chrome on url itself and returns its "
            "tab. The fill refuses when another tab in that Chrome is on the same site. A multi-step sign-in needs one call per page "
            "(a page with only a username field gets only the username). Never returns a password."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The site's sign-in page (https; http only for localhost or 127.0.0.1 with a port)."},
                "space": {"type": "string", "description": "Accepted for compatibility; this server is one virtual computer, which it uses."},
                "agent": {"type": "string", "description": "What the asking agent calls itself; shown to the user, never trusted."},
                "session": {"type": "string", "description": "Driver session of the tab to sign in (default: this client's)."},
                "target_id": {"type": "string", "description": "Browser target id from get_browser_state."},
                "tab_id": {"type": "string", "description": "Tab id from get_browser_state."},
                "username": {"type": "string", "description": "Which saved username, when the site has several."},
                "request_id": {"type": "string", "description": "The request id a previous call returned (on the retry)."},
                "wait_secs": {"type": "integer", "description": "Seconds the retry waits for the user's decision (default 20, at most 120)."},
                "profile": {"type": "string", "description": "metacomputer only: a named driver profile to open (isolated_named) when no tab is given (default a throwaway one), or the named profile of the tab given."},
                "pid": {"type": "integer", "description": "metacomputer only: the Chrome process of the tab given (the pid you bound with get_browser_state); a tab needs it, and must be in that Chrome."},
                "submit": {"type": "boolean", "description": "metacomputer only: submit the form after filling (default true)."},
                "relay_plaintext_ack": {"type": "boolean", "description": "Accepted for compatibility; nothing here travels over a relay."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "teleport_browser_session",
        "description": (
            "Load the user's signed-in session for specific sites into the virtual computer's page-mode Chrome: the "
            "cookies they imported from their own Chrome with `metacomputer secrets import-session`, sealed in their "
            "vault. Name each site; there is no 'every site'. The first call answers consent_required with a "
            "request_id while the user approves the load (Touch ID, password, passphrase or their unlock terminal); only "
            "the cookies those sites get are loaded. Call again with the request_id to get moved: true and the driver "
            "profile to open with browser_prepare "
            "({mode: \"isolated_named\", name: profile}). If no imported session covers the sites, it says which command "
            "the user runs in their own terminal to import one. Chrome only (no Firefox); cookies only, not localStorage. "
            "Use it only when the user asks to browse as themselves."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sites": {"type": "array", "items": {"type": "string"}, "description": "The exact sites, for example [\"github.com\"]."},
                "browser": {"type": "string", "enum": ["chrome", "firefox"], "description": "chrome (the only one supported here)."},
                "name": {"type": "string", "description": "Accepted for compatibility (a sandbox); this server is one virtual computer."},
                "reason": {"type": "string", "description": "Why, in a few words; shown to the user, never trusted."},
                "request_id": {"type": "string", "description": "The request id from the first call (on the retry)."},
                "duration_minutes": {"type": "integer", "description": "Accepted for compatibility; loaded cookies stay in the profile until it's deleted or the VM is reset."},
                "session": {"type": "string", "description": "metacomputer only: the sealed session's name, when several cover the sites."},
                "wait_secs": {"type": "integer", "description": "metacomputer only: seconds the retry waits for the user (default 20, at most 120)."},
            },
            "required": ["sites"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}

LOCKED = ("The user's secrets vault is locked. Ask them to run `metacomputer secrets unlock` in their own terminal "
          "(never ask for the passphrase or a password in chat), then call again.")


def with_tools(tools):
    """The driver's tools plus these two (once)."""
    tools = list(tools or [])
    have = {t.get("name") for t in tools if isinstance(t, dict)}
    return tools + [t for t in TOOLS if t["name"] not in have]


# --- small helpers ---------------------------------------------------------------------------------
def canonical_origin(url):
    """https://host[:port] of a page URL (http only for localhost / 127.0.0.1 with a port), as the vault writes
    origins; None when it isn't one the vault accepts."""
    m = re.match(r"^(https?)://([^/?#@\s]+)(?:[/?#]|$)", str(url or ""), re.I)
    if not m:
        return None
    scheme, hostport = m.group(1).lower(), m.group(2).lower()
    host, _, port = hostport.rpartition(":") if re.search(r":\d+$", hostport) else (hostport, "", "")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
        return None
    if port and not (port.isdigit() and 1 <= int(port) <= 65535):
        return None
    if scheme == "https":
        return "https://" + host + ("" if not port or port == "443" else ":" + port)
    if host in ("127.0.0.1", "localhost") and port:
        return "http://" + host + ("" if port == "80" else ":" + port)
    return None


def mask(u):
    u = str(u or "")
    if not u:
        return ""
    at = u.rfind("@")
    return u[0] + "***" + (u[at:] if 0 < at < len(u) - 1 else "")


def clean(s, n=200):
    return re.sub(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]", "?", str(s or ""))[:n]


def attrs(label):
    """dom_refs_v1's label ("name=username id=username type=email") as a dict."""
    d = {}
    for part in str(label or "").split():
        if "=" in part:
            k, v = part.split("=", 1)
            d.setdefault(k, v)
    return d


def css_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def selector(a, tag="input"):
    if a.get("id") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", a["id"]):
        return "#" + a["id"]
    if a.get("name"):
        return f"{tag}[name={css_str(a['name'])}]"
    if a.get("id"):
        return f"[id={css_str(a['id'])}]"
    return None


def find_fields(refs):
    """From a dom_refs_v1 snapshot: the first password field of the page itself (not a frame), the text, email or
    phone field nearest before it, and a submit button after it. Returns (password, username, submit) as
    (selector, ref) pairs or None."""
    main = [(i, r, attrs(r.get("label"))) for i, r in enumerate(refs or []) if r.get("frame") in ("main", None)]
    inputs = [(i, r, a) for i, r, a in main if r.get("node") == "input"]
    pws = [(i, r, a) for i, r, a in inputs if a.get("type", "").lower() == "password"]
    texty = ("", "text", "email", "tel")
    pw = user = submit = None
    if pws:
        i, r, a = pws[0]
        sel = selector(a) or ('input[type="password"]' if len(pws) == 1 else None)
        pw = (sel, r.get("ref")) if sel else None
        before = [(j, rr, aa) for j, rr, aa in inputs if j < i and aa.get("type", "").lower() in texty]
        if before:
            _, rr, aa = before[-1]
            s = selector(aa)
            user = (s, rr.get("ref")) if s else None
        after = [(j, rr, aa) for j, rr, aa in main if j > i and (
            (rr.get("node") == "button" and aa.get("type", "submit").lower() == "submit")
            or (rr.get("node") == "input" and aa.get("type", "").lower() == "submit"))]
        if after:
            submit = (None, after[0][1].get("ref"))
    else:
        users = [(j, rr, aa) for j, rr, aa in inputs if aa.get("type", "").lower() in texty]
        if users:
            _, rr, aa = users[0]
            s = selector(aa)
            user = (s, rr.get("ref")) if s else None
            after = [(j, r2, a2) for j, r2, a2 in main if j > users[0][0] and (
                (r2.get("node") == "button" and a2.get("type", "submit").lower() == "submit")
                or (r2.get("node") == "input" and a2.get("type", "").lower() == "submit"))]
            if after:
                submit = (None, after[0][1].get("ref"))
    return pw, user, submit


def last_json(text):
    for line in reversed(str(text or "").strip().splitlines()):
        try:
            v = json.loads(line)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


class ToolError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# --- requests in flight -----------------------------------------------------------------------------
class Job:
    def __init__(self, kind, owner, info):
        self.kind, self.owner, self.info = kind, owner, info
        self.created = time.time()
        self.done = threading.Event()
        self.result = None


JOBS = {}
JOBS_LOCK = threading.Lock()


def new_request_id():
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items() if time.time() - j.created > JOB_TTL_S]:
            JOBS.pop(k, None)
        while True:
            rid = _random.token_hex(4)
            if rid not in JOBS:
                return rid


def owner_of(client):
    """Whose request it is: the client's Claude Code session, else the process that made the call."""
    return getattr(client, "claude", None) or getattr(client, "holder", None) or getattr(client, "peer_pid", None)


# --- holding the computer for the work ------------------------------------------------------------------
_current = threading.local()      # the Hold the work on this thread belongs to (driver() names it to the hub)


class Hold:
    """The computer held for one of these tool calls and the work it starts in the background (the vault's question,
    the fill, the submit): while it lasts, it counts as the client's acting call, so if the client disconnects the hub
    keeps the computer for it under the client's lease (hub.job_start); past the lease's life the hub stops it (the
    vault call is ended). end() is idempotent."""

    def __init__(self, hub, client, name):
        self.hub, self.lock, self.ended, self.proc, self.stopped = hub, threading.Lock(), False, None, False
        self.background = False
        start = getattr(hub, "job_start", None)
        self.key = start(client, name, self.stop) if start else None

    def stop(self):
        with self.lock:
            self.stopped = True
            proc = self.proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def end(self):
        with self.lock:
            if self.ended:
                return
            self.ended = True
        if self.key is not None:
            self.hub.job_end(self.key)


# --- the vault and the driver -----------------------------------------------------------------------
def vault(hub, *args, timeout=330, hold=None):
    """`metacomputer secrets ARGS` with the hub's settings; (exit code, its JSON answer, its stderr). A hold that is
    stopped (past its lease's life) ends the vault call."""
    try:
        proc = subprocess.Popen([CLI, "secrets", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(hub.env), stdin=subprocess.DEVNULL)
    except OSError as e:
        return 4, {}, f"could not run metacomputer secrets ({type(e).__name__})"
    if hold is not None:
        with hold.lock:
            hold.proc = proc
            stopped = hold.stopped
        if stopped:
            proc.kill()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 4, {}, "metacomputer secrets didn't finish in time"
    finally:
        if hold is not None:
            with hold.lock:
                hold.proc = None
    return proc.returncode, last_json(out), err.strip()


def driver(hub, client, tool, args, timeout=60):
    """One driver call the hub makes for this client (the action log names the client and records the call as
    secret-adjacent); its structured result. Its errors are this module's own words: the VM may hold a value by now
    and could spell it in an error message or a result's text, so none of that goes back to the client."""
    tool_name = re.sub(r"[^A-Za-z0-9_]", "", str(tool))[:40]
    hold = getattr(_current, "hold", None)
    if hold is not None and hold.stopped:
        raise ToolError("driver", "stopped: the computer was held past its lease's life")
    m = hub.call_for(client, tool, args, timeout=timeout, job=hold.key if hold is not None else None)
    if m is None:
        raise ToolError("driver", f"the virtual computer didn't answer {tool_name} in time")
    if "error" in m:
        raise ToolError("driver", f"{tool_name} failed (the driver's own words aren't passed on during a sign-in or session load)")
    res = m.get("result") or {}
    if not isinstance(res, dict):
        raise ToolError("driver", f"{tool_name} gave an answer in an unexpected form")
    if res.get("isError"):
        try:
            hub.note_driver_refusal(client, tool, res)
        except Exception:
            pass
        raise ToolError("driver", f"{tool_name} was refused (the driver's own words aren't passed on during a sign-in or session load)")
    sc = res.get("structuredContent")
    return sc if isinstance(sc, dict) else {}


def session_label(client, args):
    s = args.get("session")
    if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", s):
        return s
    if getattr(client, "session", None):
        return client.session
    if getattr(client, "auto_session", None):
        client.used_auto = True
        return client.auto_session
    return "site-login"


def open_page(hub, client, sess, url, profile=None):
    """Page-mode Chrome on url (a driver-owned profile): its target and tab."""
    prof = {"mode": "isolated_named", "name": profile} if profile else {"mode": "isolated_new"}
    sc = driver(hub, client, "browser_prepare", {"session": sess, "allow_launch": True, "profile": prof}, timeout=90)
    pid = sc.get("prepared_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ToolError("driver", "browser_prepare didn't start Chrome")
    win = None
    for _ in range(40):
        ws = (driver(hub, client, "list_windows", {"pid": pid}) or {}).get("windows") or []
        if ws:
            win = ws[0]
            break
        time.sleep(0.25)
    if not win:
        raise ToolError("driver", "Chrome's window didn't appear")
    b = driver(hub, client, "get_browser_state", {"pid": pid, "window_id": win["window_id"], "session": sess})
    tabs = b.get("tabs") or []
    if not b.get("target_id") or not tabs:
        raise ToolError("driver", "Chrome's tab couldn't be bound")
    tgt = {"target_id": b["target_id"], "tab_id": tabs[0]["tab_id"], "session": sess}
    driver(hub, client, "browser_navigate", dict(tgt, url=url), timeout=60)
    return tgt, pid


def tab_in_chrome(hub, client, sess, tgt, pid):
    """Is this target and tab one of the windows of Chrome process pid, as the driver binds them?"""
    ws = (driver(hub, client, "list_windows", {"pid": pid}) or {}).get("windows") or []
    for w in ws[:20]:
        if not isinstance(w, dict) or w.get("pid") not in (None, pid):
            continue
        b = driver(hub, client, "get_browser_state", {"pid": pid, "window_id": w.get("window_id"), "session": sess}) or {}
        if b.get("target_id") == tgt["target_id"] and any(isinstance(t, dict) and t.get("tab_id") == tgt["tab_id"] for t in b.get("tabs") or []):
            return True
    return False


def snapshot(hub, client, tgt, want_origin=None, wait=15):
    """dom_refs_v1 of the tab, once the page has some fields (or the wait is over)."""
    end, snap = time.time() + wait, {}
    while True:
        snap = driver(hub, client, "get_browser_state", dict(tgt, snapshot_format="dom_refs_v1"))
        here = canonical_origin(snap.get("url"))
        if (want_origin is None or here == want_origin) and any(r.get("node") == "input" for r in snap.get("refs") or []):
            return snap
        if time.time() >= end:
            return snap
        time.sleep(0.6)


# --- request_site_login ------------------------------------------------------------------------------
def site_login(hub, client, args, hold=None):
    url = args.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        raise ToolError("invalid_argument", "url: the site's sign-in page")
    origin = canonical_origin(url)
    if not origin:
        raise ToolError("invalid_argument", "url: an https page (http only for localhost or 127.0.0.1 with a port)")
    try:
        wait = max(0, min(120, int(args.get("wait_secs", 20))))
    except (TypeError, ValueError):
        raise ToolError("invalid_argument", "wait_secs: a number of seconds, 0 to 120")
    rid = args.get("request_id")
    if rid is not None:
        return collect(client, rid, "login", wait, origin=origin)

    rc, listed, err = vault(hub, "list", "--json", timeout=30)
    if rc == 3:
        raise ToolError("locked", LOCKED)
    if rc != 0:
        raise ToolError("vault", clean(err or "the vault couldn't be read", 300))
    logins = [s for s in listed.get("secrets") or [] if s.get("kind") == "password" and origin in (s.get("origins") or [])]
    want = args.get("username")
    if isinstance(want, str) and want:
        logins = [s for s in logins if s.get("username") == want]
    if not logins:
        raise ToolError("not_found", (f"no saved login for {origin}" + (" with that username" if want else "")
                                      + ". The user can bring theirs in, in their own terminal: `metacomputer secrets "
                                      "import-passwords --browser chrome --site " + origin.split("://", 1)[1].split(":")[0]
                                      + "` (or --csv for an export), or store one with `metacomputer secrets set`."))
    if len(logins) > 1:
        raise ToolError("invalid_argument", "several saved logins for " + origin + ": pass username (one of "
                        + ", ".join(mask(s.get("username")) or "(none)" for s in logins) + ")")
    secret = logins[0]
    sess = session_label(client, args)
    profile = args.get("profile") if isinstance(args.get("profile"), str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.get("profile")) else None
    bpid = args.get("pid") if isinstance(args.get("pid"), int) and not isinstance(args.get("pid"), bool) and args.get("pid") > 1 else None
    # The vault is told which Chrome the hub read (its pid), and the fill refuses when another tab there is on the site:
    # a value can't go to a same-site tab in another Chrome. A tab passed in must be in the Chrome passed with it, as the
    # driver binds them, or nothing is typed.
    if args.get("target_id") and args.get("tab_id"):
        if not bpid:
            raise ToolError("invalid_argument", "a tab needs the Chrome it's in: pass pid (the process you bound with get_browser_state), "
                                                "or leave out the tab and let this tool open one")
        tgt = {"target_id": str(args["target_id"])[:200], "tab_id": str(args["tab_id"])[:200], "session": sess}
        if not tab_in_chrome(hub, client, sess, tgt, bpid):
            raise ToolError("login_refused", f"that tab isn't in Chrome process {bpid} (as the driver binds its windows); nothing was typed")
    else:
        tgt, bpid = open_page(hub, client, sess, url, profile)
    snap = snapshot(hub, client, tgt, origin)
    here = canonical_origin(snap.get("url"))
    if here != origin:
        raise ToolError("login_refused", f"the tab isn't on the saved login's site {origin}; nothing was typed")
    pw, user, submit = find_fields(snap.get("refs"))
    if not pw and not user:
        raise ToolError("login_refused", "no username or password field on the page (a field inside a frame needs "
                                         "`metacomputer secrets fill` after a click into it); nothing was typed")
    rid = new_request_id()
    job = Job("login", owner_of(client), {"site": origin.split("://", 1)[1], "origin": origin, "name": secret["name"]})
    with JOBS_LOCK:
        JOBS[rid] = job
    on_behalf = str(getattr(client, "peer_pid", 0) or 0)

    def work():
        _current.hold = hold
        try:
            # --origin: the vault fills only a tab on the origin checked here, not another of the login's sites.
            if pw:
                cmd = ["login", secret["name"], "--password-selector", pw[0], "--origin", origin, "--request-id", rid,
                       "--on-behalf-pid", on_behalf]
                if user and secret.get("username"):
                    cmd += ["--username-selector", user[0]]
            else:
                if not secret.get("username"):
                    raise ToolError("login_refused", "the page asks only for a username, and this login has none saved")
                cmd = ["fill", secret["name"], "--field", "username", "--selector", user[0], "--origin", origin,
                       "--request-id", rid, "--on-behalf-pid", on_behalf]
            if profile:
                cmd += ["--profile", profile]
            if bpid:
                cmd += ["--browser-pid", str(bpid)]
            if isinstance(args.get("agent"), str) and args["agent"] and pw:
                cmd += ["--agent", clean(args["agent"], 64)]
            rc, res, err = vault(hub, *cmd, hold=hold)
            out = {"request_id": rid, "site": job.info["site"], "space": hub.settings.get("vm_name") or ""}
            if rc == 0 and res.get("ok"):
                submitted, held = False, None
                if args.get("submit", True) is not False:
                    try:
                        # The tab must still be on the checked site: a page that moved it elsewhere gets no submit. The
                        # refs come from this fresh snapshot (a new snapshot retires the earlier one's refs).
                        snap2 = driver(hub, client, "get_browser_state", dict(tgt, snapshot_format="dom_refs_v1")) or {}
                        pw2, user2, submit2 = find_fields(snap2.get("refs"))
                        if canonical_origin(snap2.get("url")) != origin:
                            held = "the tab left the login's site after the fill, so the form wasn't submitted"
                        elif submit2 and submit2[1]:
                            driver(hub, client, "browser_click", dict(tgt, ref=submit2[1], delivery_mode="foreground"))
                            submitted = True
                        elif (pw2 or user2) and (pw2 or user2)[1]:
                            driver(hub, client, "browser_type", dict(tgt, ref=(pw2 or user2)[1], text="\n"))
                            submitted = True
                        else:
                            held = "the form's fields weren't found again after the fill, so it wasn't submitted"
                    except ToolError:
                        submitted = False
                time.sleep(1.0 if submitted else 0)
                same = False
                try:
                    # Only whether the page is still on the login's origin: its URL, and even its host, are the page's
                    # (or the VM's) to choose, and could spell the value that was just typed.
                    same = canonical_origin((driver(hub, client, "get_browser_state", dict(tgt, snapshot_format="dom_refs_v1")) or {}).get("url")) == origin
                except ToolError:
                    pass
                out.update(status="filled", origin=origin,
                           username_hint=mask(secret.get("username")), submitted=submitted, same_site=same,
                           fields=[f for f in (res.get("fields") or ["username"]) if f in ("username", "password")],
                           session=tgt["session"], target_id=tgt["target_id"], tab_id=tgt["tab_id"])
                if held:
                    out["note"] = held
            elif rc == 2:
                out.update(status="denied", error="login_refused", reason=clean(res.get("reason") or err, 300))
            elif rc == 3:
                out.update(status="error", error="locked", reason=LOCKED)
            else:
                out.update(status="error", error="login_failed", reason=clean(res.get("reason") or err, 300))
            job.result = out
        except ToolError as e:
            job.result = {"status": "error", "error": e.code, "reason": e.message, "request_id": rid}
        except Exception as e:   # never leave a request hanging
            job.result = {"status": "error", "error": "internal", "reason": f"internal error ({type(e).__name__})", "request_id": rid}
        finally:
            job.done.set()
            if hold is not None:
                hold.end()

    if hold is not None:
        hold.background = True
    threading.Thread(target=work, daemon=True).start()
    if job.done.wait(FIRST_WAIT_S):
        return finished(job)
    return {"status": "pending", "request_id": rid, "site": job.info["site"], "space": hub.settings.get("vm_name") or "",
            "approve": (f"waiting for the user: they approve in their vault's unlock terminal, or with `metacomputer secrets "
                        f"approve {rid}` (Touch ID or their login password). Call request_site_login again with "
                        f"request_id {rid} to get the outcome.")}


def finished(job):
    r = dict(job.result or {})
    if r.get("status") == "error":
        raise ToolError(r.get("error") or "error", r.get("reason") or "failed", request_id=r.get("request_id"))
    return r


def collect(client, rid, kind, wait, **_):
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{8}", rid):
        raise ToolError("invalid_argument", "request_id: the 8 hex digits a previous call returned")
    with JOBS_LOCK:
        job = JOBS.get(rid)
    if job is None or job.kind != kind:
        raise ToolError("not_found", f"no {kind.replace('_', ' ')} request {rid} here (they're kept {JOB_TTL_S // 60} minutes)")
    if job.owner != owner_of(client):
        raise ToolError("not_found", f"request {rid} belongs to another session")
    if job.done.wait(wait):
        return finished(job)
    if kind == "login":
        return {"status": "pending", "request_id": rid, "site": job.info.get("site")}
    return {"consent_required": True, "request_id": rid, "summary": job.info.get("summary")}


# --- teleport_browser_session -----------------------------------------------------------------------
def covers(session_sites, site):
    return any(site == s or site.endswith("." + s) for s in session_sites)


def teleport(hub, client, args, hold=None):
    browser = args.get("browser") or "chrome"
    if browser != "chrome":
        raise ToolError("invalid_argument", "only Chrome: the virtual computer's sealed sessions load into page-mode Chrome")
    try:
        wait = max(0, min(120, int(args.get("wait_secs", 20))))
    except (TypeError, ValueError):
        raise ToolError("invalid_argument", "wait_secs: a number of seconds, 0 to 120")
    if args.get("request_id") is not None:
        return collect(client, args["request_id"], "teleport", wait)
    sites = args.get("sites")
    if not isinstance(sites, list) or not sites or not all(isinstance(s, str) for s in sites):
        raise ToolError("invalid_argument", "sites: the exact sites, for example [\"github.com\"]")
    sites = [s.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0] for s in sites]
    if not all(re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?", s) for s in sites):
        raise ToolError("invalid_argument", "sites: host names like github.com")
    sites = [s.split(":")[0] for s in sites]
    rc, listed, err = vault(hub, "list", "--json", timeout=30)
    if rc == 3:
        raise ToolError("locked", LOCKED)
    if rc != 0:
        raise ToolError("vault", clean(err or "the vault couldn't be read", 300))
    sessions = [s for s in listed.get("sessions") or [] if s.get("kind") == "cookies"]
    want = args.get("session")
    if isinstance(want, str) and want:
        sessions = [s for s in sessions if s.get("name") == want]
    fit = [s for s in sessions if all(covers(s.get("sites") or [], site) for site in sites)]
    if not fit:
        name = re.sub(r"[^a-z0-9.-]", "-", sites[0])[:40]
        raise ToolError("not_found", (
            f"the vault has no imported session for {', '.join(sites)}. The user can import one from their own Chrome, "
            f"in their own terminal: `metacomputer secrets import-session --browser chrome "
            + " ".join(f"--site {s}" for s in sites) + f" --name {name} --to-profile {name}`. Then call this again."))
    if len(fit) > 1:
        raise ToolError("invalid_argument", "several imported sessions cover those sites: pass session (one of "
                        + ", ".join(s["name"] for s in fit) + ")")
    s = fit[0]
    profile = s.get("profile")
    rid = new_request_id()
    sites = sorted(set(sites))
    summary = (f"load {s['name']}: only the cookies for {', '.join(sites)} (of those imported for {', '.join(s.get('sites') or [])} "
               f"from {s.get('source', 'Chrome')}) into the driver profile {profile}")
    job = Job("teleport", owner_of(client), {"summary": summary, "profile": profile, "name": s["name"]})
    with JOBS_LOCK:
        JOBS[rid] = job
    on_behalf = str(getattr(client, "peer_pid", 0) or 0)

    def work():
        _current.hold = hold
        try:
            def load():
                # --site: the vault sends only the cookies those sites get (filtered on the Mac), whatever else the session holds.
                return vault(hub, "session", "load", s["name"], *[a for x in sites for a in ("--site", x)], "--request-id", rid,
                             "--on-behalf-pid", on_behalf, hold=hold)
            rc, res, err = load()
            if rc != 0 and "open it once" in (res.get("reason") or err or ""):
                # The profile has no cookie store yet: let the driver create it, close it, then load (asked only now).
                tmp = f"mcomp-teleport-{rid}"
                driver(hub, client, "browser_prepare", {"session": tmp, "allow_launch": True,
                                                        "profile": {"mode": "isolated_named", "name": profile}}, timeout=90)
                time.sleep(6)
                driver(hub, client, "end_session", {"session": tmp}, timeout=60)
                time.sleep(1)
                rc, res, err = load()
            if rc == 0 and res.get("ok"):
                job.result = {"moved": True, "request_id": rid, "session": s["name"], "sites": sites,
                              "cookies": res.get("cookies"), "profile": profile,
                              "next": (f"browser_prepare with profile {{mode: \"isolated_named\", name: \"{profile}\"}} and "
                                       "allow_launch: true opens Chrome with these cookies"),
                              "note": "cookies only: localStorage wasn't imported; they stay in the profile until it's deleted or the VM is reset"}
            elif rc == 2:
                job.result = {"status": "error", "error": "denied", "reason": clean(res.get("reason") or err, 300), "request_id": rid}
            elif rc == 3:
                job.result = {"status": "error", "error": "locked", "reason": LOCKED, "request_id": rid}
            else:
                job.result = {"status": "error", "error": "teleport_refused", "reason": clean(res.get("reason") or err, 300), "request_id": rid}
        except ToolError as e:
            job.result = {"status": "error", "error": e.code, "reason": e.message, "request_id": rid}
        except Exception as e:
            job.result = {"status": "error", "error": "internal", "reason": f"internal error ({type(e).__name__})", "request_id": rid}
        finally:
            job.done.set()
            if hold is not None:
                hold.end()

    if hold is not None:
        hold.background = True
    threading.Thread(target=work, daemon=True).start()
    if job.done.wait(FIRST_WAIT_S):
        return finished(job)
    return {"consent_required": True, "request_id": rid, "summary": summary,
            "approve": (f"waiting for the user: they approve in their vault's unlock terminal, or with `metacomputer secrets "
                        f"approve {rid}` (Touch ID or their login password). Call teleport_browser_session again with "
                        f"request_id {rid}.")}


# --- the hub's entry point ------------------------------------------------------------------------------
def call(hub, client, msg):
    """Serve one of these tools (on a thread of its own: it waits on the driver and the vault)."""
    params = msg.get("params") or {}
    name, args = params.get("name"), params.get("arguments") or {}

    hold = Hold(hub, client, name)

    def run():
        _current.hold = hold
        try:
            out = site_login(hub, client, args, hold) if name == "request_site_login" else teleport(hub, client, args, hold)
            hub.reply(client, msg, {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out})
            what = "signed in" if out.get("status") == "filled" else ("loaded a session" if out.get("moved") else "is waiting for the user")
            hub.note(f"{name} for client {client.cid}: {what}")
        except ToolError as e:
            sc = dict({"error": e.code, "message": e.message}, **{k: v for k, v in e.extra.items() if v is not None})
            hub.tool_error(client, msg, f"{name}: {e.message}", sc)
        except Exception as e:
            hub.tool_error(client, msg, f"{name} failed: internal error ({type(e).__name__})")
        finally:
            if not hold.background:
                hold.end()           # no background work started: the hold ends with the call

    threading.Thread(target=run, daemon=True).start()
