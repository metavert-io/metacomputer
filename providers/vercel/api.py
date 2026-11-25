"""Vercel's Sandbox REST API, as metacomputer uses it: a small client of the endpoints Vercel's own SDK calls
(@vercel/sandbox 4.1: /v2/sandboxes, /v3/sandboxes, /v2/sandboxes/sessions/ID/..., /v2/sandboxes/snapshots), as plain
values (dicts, strings), so a stand-in answers for it in the tests.

Its credentials are given as values (lib/remote.py hands them over from its own environment, which nothing else
inherits; the transport's broker gets them on a pipe): VERCEL_TOKEN (an access token: team-wide, see docs/vercel.md),
VERCEL_TEAM_ID and VERCEL_PROJECT_ID (the team and the project its sandboxes are made in). None of them is ever put in
a message: everything Vercel says passes scrub() (the token, the team's and project's IDs and anything
credential-shaped replaced).

HTTP goes through `http` (lib/provider_http.py send, or a keep-alive session of it in the broker; a test's stand-in),
and a command's output stream through `stream`: nothing here opens a connection. A call that makes or changes
something is sent once (lib/cloud/vps.py API.call); reads and deletes are tried again on throttling and passing
errors."""
import contextlib
import json
import re

from cloud.adapter import AdapterError
from cloud.vps import API as RestAPI, ProviderError, query_string

HOST = "api.vercel.com"
BASE = "https://" + HOST
TEAM_ID = re.compile(r"team_[A-Za-z0-9]{8,64}")
PROJECT_ID = re.compile(r"prj_[A-Za-z0-9]{8,64}")
SANDBOX_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
SESSION_ID = re.compile(r"sbx_[A-Za-z0-9]{8,64}")
SNAPSHOT_ID = re.compile(r"snap_[A-Za-z0-9]{8,64}")
COMMAND_ID = re.compile(r"cmd_[A-Za-z0-9]{8,64}")
MAX_PAGES = 50


class VercelError(ProviderError):
    """Vercel refused a call: its status, its error code ("not_found", "sandbox_timeout_invalid"...) and its
    message, scrubbed."""


def check_ids(team, project):
    if not TEAM_ID.fullmatch(team or ""):
        raise AdapterError("VERCEL_TEAM_ID isn't a Vercel team's ID (team_...): the team the sandboxes are made in "
                           "(docs/vercel.md)")
    if not PROJECT_ID.fullmatch(project or ""):
        raise AdapterError("VERCEL_PROJECT_ID isn't a Vercel project's ID (prj_...): the project the sandboxes are "
                           "made in (docs/vercel.md)")


def sandbox_name(n):
    if not isinstance(n, str) or not SANDBOX_NAME.fullmatch(n):
        raise AdapterError(f"{str(n)[:64]!r} isn't a sandbox's name (lowercase letters, digits and dashes)")
    return n


def _id(kind, v):
    pat = {"session": SESSION_ID, "snapshot": SNAPSHOT_ID, "command": COMMAND_ID}[kind]
    if not isinstance(v, str) or not pat.fullmatch(v):
        raise AdapterError(f"Vercel gave a {kind} ID that isn't one")
    return v


class API(RestAPI):
    """The Sandbox API of one team and project."""
    label = "Vercel"
    base = BASE
    env_key = "VERCEL_TOKEN"

    def __init__(self, env=None, http=None, stream=None, sleep=None, clock=None):
        env = env or {}
        super().__init__(env, http, sleep, clock)
        team, project = (env.get("VERCEL_TEAM_ID") or "").strip(), (env.get("VERCEL_PROJECT_ID") or "").strip()
        check_ids(team, project)
        self.team, self.project = team, project
        self.__tok = env["VERCEL_TOKEN"].strip()
        self._stream = stream

    def scrub(self, text, cap=300):
        out = super().scrub(text, cap * 2)
        for v in (self.team, self.project):
            if v:
                out = out.replace(v, v[:5] + "...")
        return out[:cap]

    def call(self, method, path, body=None, query=None, missing_ok=False, what=""):
        """One call, in this team (teamId on every request)."""
        return super().call(method, path, body=body, query=dict(query or {}, teamId=self.team), missing_ok=missing_ok,
                            what=what)

    def refused(self, status, answer, what=""):
        code, message = "", ""
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            j = json.loads(answer)
            err = j.get("error") if isinstance(j.get("error"), dict) else j
            code, message = err.get("code", ""), err.get("message", "")
        return VercelError(self.label, status, code, message, what)

    # --- the account -----------------------------------------------------------------------------------------
    def plan(self):
        """The team's plan: "hobby", "pro", "enterprise", or "" when it can't be read."""
        try:
            j = self.call("GET", f"/v2/teams/{self.team}", what="reading the team")
        except AdapterError:
            return ""
        p = str(((j or {}).get("billing") or {}).get("plan") or "").lower()
        return p if re.fullmatch(r"[a-z]{2,20}", p) else ""

    # --- sandboxes ---------------------------------------------------------------------------------------------
    def sandbox(self, name, resume=False):
        """{"sandbox": ..., "session": ...} for the sandbox of that name in the project, or None when there's none.
        resume=True starts a new session when it's stopped (anything else never does)."""
        q = {"projectId": self.project}
        if resume:
            j = self.resume_call(f"/v2/sandboxes/{sandbox_name(name)}", dict(q, resume="true"))
        else:
            j = self.call("GET", f"/v2/sandboxes/{sandbox_name(name)}", query=q, missing_ok=True,
                          what="reading the sandbox")
        if j is None or not isinstance(j.get("sandbox"), dict):
            return None
        return {"sandbox": j["sandbox"], "session": j.get("session") if isinstance(j.get("session"), dict) else {}}

    RESUME_TIMEOUT_S = 900

    def resume_call(self, path, query):
        """GET that starts a session: it answers once the session runs, which took 0.8 s after a stop but 4 to 5
        minutes after a restore (a snapshot that wasn't the latest: measured live twice), so it's waited for that long.
        Sent again (it's idempotent: a running session is returned as it is) only when the connection failed."""
        url = self.base + path + "?" + query_string(dict(query, teamId=self.team))
        headers = {"Authorization": f"Bearer {self.__tok}", "Accept": "application/json", "User-Agent": "metacomputer"}
        for attempt in range(3):
            try:
                status, _h, answer = self._http("GET", url, headers, None, self.RESUME_TIMEOUT_S)
            except OSError as e:
                if attempt == 2:
                    raise AdapterError(f"Vercel's API didn't answer ({self.scrub(e)})") from None
                self._sleep(2 ** attempt)
                continue
            if status == 404:
                return None
            if 200 <= status < 300:
                try:
                    return json.loads(answer)
                except ValueError:
                    raise AdapterError("Vercel's API answered with something that isn't JSON") from None
            if (status == 429 or status >= 500) and attempt < 2:
                self._sleep(2 ** attempt)
                continue
            raise self._scrubbed(self.refused(status, answer, "starting a session"))
        return None

    def create(self, name, image, vcpus, region, timeout_ms, tags, network_policy):
        """A persistent sandbox (its disk kept at every stop), its snapshots never expiring by themselves."""
        body = {"projectId": self.project, "name": sandbox_name(name), "image": image, "resources": {"vcpus": vcpus},
                "timeout": int(timeout_ms), "persistent": True, "snapshotExpiration": 0, "tags": dict(tags),
                "networkPolicy": network_policy}
        if region:
            body["region"] = region
        j = self.call("POST", "/v3/sandboxes", body=body, what="making the sandbox")
        if not isinstance(j.get("sandbox"), dict):
            raise AdapterError("Vercel made the sandbox but didn't describe it")
        return {"sandbox": j["sandbox"], "session": j.get("session") if isinstance(j.get("session"), dict) else {}}

    def update(self, name, **fields):
        j = self.call("PATCH", f"/v2/sandboxes/{sandbox_name(name)}", query={"projectId": self.project}, body=fields,
                      what="changing the sandbox")
        return j.get("sandbox") or {}

    def delete(self, name):
        """The sandbox, its sessions and the snapshots no other sandbox uses (True), or False when it was gone."""
        j = self.call("DELETE", f"/v2/sandboxes/{sandbox_name(name)}", query={"projectId": self.project,
                                                                              "deleteOrphanSnapshots": "true"},
                      missing_ok=True, what="deleting the sandbox")
        return j is not None

    def sandboxes(self, prefix=None, tag=None):
        """The project's sandboxes (each a dict), by name prefix or one tag ("key:value")."""
        out, cursor = [], None
        for _ in range(MAX_PAGES):
            q = {"project": self.project, "limit": 50, "cursor": cursor}
            if prefix:
                q.update(namePrefix=prefix, sortBy="name")
            if tag:
                q["tags"] = tag
            j = self.call("GET", "/v2/sandboxes", query=q, what="listing the sandboxes")
            out += [s for s in j.get("sandboxes") or [] if isinstance(s, dict)]
            cursor = (j.get("pagination") or {}).get("next")
            if not cursor:
                break
        return out

    # --- sessions --------------------------------------------------------------------------------------------
    def stop(self, session):
        """Stops a session (a persistent sandbox's disk is snapshotted first): {"session", "sandbox", "snapshot"}."""
        return self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/stop", body={}, what="stopping it")

    def session(self, session):
        j = self.call("GET", f"/v2/sandboxes/sessions/{_id('session', session)}", missing_ok=True,
                      what="reading the session")
        return (j or {}).get("session")

    def extend(self, session, ms):
        """More time for the session (up to the plan's cap): its session afterwards."""
        j = self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/extend-timeout",
                      body={"duration": int(ms)}, what="extending its session")
        return j.get("session") or {}

    def set_policy(self, name, policy, session=None):
        """The sandbox's network policy. With its running session (`session`): in force on it at once, and kept for
        the next ones (Vercel saves a live update of the current session). Without one (stopped): kept for the next.
        A change of the sandbox alone (PATCH) doesn't reach a running session (found live: the old policy stayed in
        force)."""
        if session:
            j = self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/network-policy", body=policy,
                          what="changing the network policy")
            return (j.get("session") or {}).get("networkPolicy")
        return self.update(name, networkPolicy=policy).get("networkPolicy")

    # --- snapshots -------------------------------------------------------------------------------------------
    def snapshot(self, session):
        """A snapshot of the session's disk, never expiring by itself. It stops the session."""
        return self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/snapshot",
                         body={"expiration": 0}, what="taking a snapshot")

    def snapshots(self, name):
        """Every snapshot of the sandbox of that name (deleted ones included, as Vercel lists them)."""
        out, cursor = [], None
        for _ in range(MAX_PAGES):
            j = self.call("GET", "/v2/sandboxes/snapshots", query={"project": self.project, "name": sandbox_name(name),
                                                                   "limit": 50, "cursor": cursor},
                          what="listing its snapshots")
            out += [s for s in j.get("snapshots") or [] if isinstance(s, dict)]
            cursor = (j.get("pagination") or {}).get("next")
            if not cursor:
                break
        return out

    def get_snapshot(self, snap):
        j = self.call("GET", f"/v2/sandboxes/snapshots/{_id('snapshot', snap)}", missing_ok=True,
                      what="reading a snapshot")
        return (j or {}).get("snapshot")

    def delete_snapshot(self, snap):
        """True once it's deleted (or was), False when Vercel has no such snapshot."""
        j = self.call("DELETE", f"/v2/sandboxes/snapshots/{_id('snapshot', snap)}", missing_ok=True,
                      what="deleting a snapshot")
        return j is not None

    # --- commands --------------------------------------------------------------------------------------------
    def start(self, session, argv, sudo=False, stdin=False, timeout_ms=None):
        """A command in the session (argv[0] and its arguments, never through a shell): its command ID."""
        if not argv or not all(isinstance(a, str) for a in argv):
            raise AdapterError("a command is a list of words")
        body = {"command": argv[0], "args": list(argv[1:]), "sudo": bool(sudo)}
        if stdin:
            body["attachStdin"] = True
        if timeout_ms:
            body["timeout"] = int(timeout_ms)
        j = self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/cmd", body=body,
                      what="running a command")
        return _id("command", (j.get("command") or {}).get("id"))

    def write(self, session, cmd, data, offset, close=False):
        """Bytes to the command's standard input, at `offset` (safe to send again: Vercel skips what it has)."""
        body = {"offset": int(offset)}
        if data:
            import base64
            body["data"] = base64.b64encode(bytes(data)).decode()
        if close:
            body["close"] = True
        j = self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/cmd/{_id('command', cmd)}/stdin",
                      body=body, what="writing to a command")
        return j.get("bytesWritten")

    def kill(self, session, cmd, signal=15):
        self.call("POST", f"/v2/sandboxes/sessions/{_id('session', session)}/cmd/{_id('command', cmd)}/kill",
                  body={"signal": int(signal)}, what="ending a command")

    def result(self, session, cmd):
        """The command's exit code, once it has ended (waits for it)."""
        j = self.call("GET", f"/v2/sandboxes/sessions/{_id('session', session)}/cmd/{_id('command', cmd)}",
                      query={"wait": "true"}, what="reading a command's result")
        code = (j.get("command") or {}).get("exitCode")
        return code if isinstance(code, int) else None

    def logs_url(self, session, cmd):
        """Where the command's output is read as it comes (one JSON object a line), and the headers: for `stream`."""
        path = f"/v2/sandboxes/sessions/{_id('session', session)}/cmd/{_id('command', cmd)}/logs"
        return (self.base + path + "?" + query_string({"teamId": self.team}),
                {"Authorization": f"Bearer {self.__tok}", "User-Agent": "metacomputer", "Accept": "application/x-ndjson"})

    def logs(self, session, cmd, timeout=60):
        """The command's output as (stream, text) pairs, read as they come, until it ends."""
        if self._stream is None:
            raise AdapterError("no way to read a command's output as it comes was given (lib/remote.py gives it)")
        url, headers = self.logs_url(session, cmd)
        for line in self._stream("GET", url, headers, None, timeout):
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                raise AdapterError("Vercel sent a command's output in a form this doesn't read") from None
            if not isinstance(j, dict):
                continue
            if j.get("stream") == "error":
                d = j.get("data") if isinstance(j.get("data"), dict) else {}
                raise self._scrubbed(VercelError(self.label, 0, d.get("code", ""), d.get("message", ""),
                                                 "reading a command's output"))
            if j.get("stream") in ("stdout", "stderr") and isinstance(j.get("data"), str):
                yield j["stream"], j["data"]

    def run(self, session, argv, sudo=False, stdin=None, timeout_ms=None, max_output=1 << 20):
        """A command run to its end: (exit code, stdout bytes, stderr bytes). Its standard input (bytes) is sent
        first, then closed. Output past max_output ends the command (the rest isn't read)."""
        cmd = self.start(session, argv, sudo=sudo, stdin=stdin is not None, timeout_ms=timeout_ms)
        if stdin is not None:
            off, data = 0, bytes(stdin)
            while off < len(data):
                chunk = data[off:off + (1 << 20)]
                self.write(session, cmd, chunk, off)
                off += len(chunk)
            self.write(session, cmd, b"", off, close=True)
        out, err, over = bytearray(), bytearray(), False
        for s, text in self.logs(session, cmd, timeout=max(60, (timeout_ms or 0) // 1000 + 30)):
            (out if s == "stdout" else err).extend(text.encode())
            if len(out) + len(err) > max_output:
                over = True
                break
        if over:
            with contextlib.suppress(AdapterError):
                self.kill(session, cmd, 9)
            return 124, bytes(out[:max_output]), bytes(err[-65536:]) + b"\n(output past its cap: the command was ended)"
        return self.result(session, cmd), bytes(out), bytes(err)
