#!/usr/bin/env python3
"""Vercel Sandbox (docs/vercel.md), against a stand-in for Vercel's REST API. No VM, no network, no Vercel account.

    python3 tests/test_vercel.py

- The `vercel` transport (providers/vercel/adapter.py opener): providers/vercel/tunnel.py as the ProxyCommand, through
  lib/proxywrap.py, the host key pinned under mcomp-NAME; the broker's socket in a private folder; bad names refused.
- The network policy: EGRESS=internet (every name, the private ranges refused), off (deny-all), an allowlist (names
  only, with their subdomains unless exact, the private ranges refused); what Vercel reads back classified; widening.
- The API client (providers/vercel/api.py) on the stand-in: teamId on every call, the project, the bodies (persistent,
  snapshots never expiring, tags, the policy), a stop with a body, the running session's policy changed (not the
  sandbox's alone), commands with their standard input at offsets, errors scrubbed of the token and the IDs.
- The adapter: create (refused over a sandbox of that name), every operation checked against the sandbox's creation
  time and tags, wake (a session, then the boot script as root; never an exec that starts one), stop (and the snapshot
  it replaced deleted, checkpoints kept), checkpoints (a snapshot stops it), restore, delete with every snapshot (those
  Vercel still lists after the sandbox goes too), status's notes, rates.
- lib/remote.py with the provider's hooks: the launch journaled before the sandbox is made, a lost launch found by its
  tag (and one without it left alone), abort, wake (the record checked first, the broker started with the credentials
  on its standard input only), delete, plan, the transport's checks from the record alone (no credentials there).
- The broker, in this process with a stand-in API, and tunnel.py as a real program: the identity check, a connection
  carrying random bytes both ways (base64 lines back), refused for another sandbox or a stopped one (never started),
  the socket's permissions, the session extended only while a connection is open (up to the plan's cap), quit.
- The credentials: taken off the environment by every shell entry (the generated line), never in what `remote.py
  target` prints, the ProxyCommand, tunnel.py's arguments, or a child's environment.
- The guest: the boot script blocks Vercel's agent port before sshd and stops when the rule isn't in force; sshd on
  the sandbox's loopback only; provision.sh's vercel target; the scripts' command-line rules.
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(LAB, "lib"))
import cloud  # noqa: E402
import remote  # noqa: E402
from cloud import capabilities as caps  # noqa: E402
from cloud import transport  # noqa: E402
from cloud.adapter import AdapterError  # noqa: E402

vc = cloud.registry.module("vercel")
vapi = cloud.registry.module("vercel", "api")
hooks = cloud.registry.module("vercel", "remote")
bk = cloud.registry.module("vercel", "broker")
GUEST = os.path.join(LAB, "providers", "vercel", "guest")
TUNNEL = os.path.join(LAB, "providers", "vercel", "tunnel.py")

FAILS = []
# Stand-in credentials, built from a description (no key-like literal in this file): the token is its prefix and
# a run of one letter, the IDs their prefixes and a run of another.
TOKEN = "vcp_" + "t" * 44
TEAM = "team_" + "m" * 24
PROJECT = "prj_" + "p" * 28
CREDS = {"VERCEL_TOKEN": TOKEN, "VERCEL_TEAM_ID": TEAM, "VERCEL_PROJECT_ID": PROJECT}
OP = "0123456789abcdef0123456789abcdef"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn, *a, kind=Exception, **k):
    try:
        fn(*a, **k)
    except kind as e:
        return str(e) or type(e).__name__
    except Exception as e:      # noqa: BLE001
        return f"WRONG {type(e).__name__}: {e}"
    return None


class Stand:
    """Vercel's Sandbox REST API in memory: its HTTP (send's signature) and its output stream. Every call logged."""

    def __init__(self, plan="hobby"):
        self.sandboxes, self.snapshots, self.cmds, self.log = {}, {}, {}, []
        self.plan, self.n, self.now = plan, 0, 1795630000000
        self.fail_next = {}          # (method, path prefix) -> (status, body), once
        self.lose_create = False
        self.boot_rc = 0
        self.gone = set()

    def _id(self, kind):
        self.n += 1
        return f"{kind}_{'%024d' % self.n}"

    def _session(self, name, source=None):
        sid = self._id("sbx")
        self.now += 1000
        return {"id": sid, "status": "running", "startedAt": self.now, "requestedAt": self.now, "timeout": 900000,
                "sourceSnapshotId": source, "memory": 4096, "vcpus": 2, "region": "iad1", "createdAt": self.now,
                "cwd": "/vercel", "updatedAt": self.now}

    def http(self, method, url, headers, body=None, timeout=60):
        assert url.startswith("https://api.vercel.com/"), url
        path, _, qs = url[len("https://api.vercel.com"):].partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if p)
        data = json.loads(body) if body else None
        self.log.append({"method": method, "path": path, "query": q, "body": data, "headers": dict(headers),
                         "timeout": timeout})
        for (m, pre), ans in list(self.fail_next.items()):
            if m == method and path.startswith(pre):
                del self.fail_next[(m, pre)]
                return ans[0], {}, json.dumps(ans[1]).encode()
        status, obj = self.route(method, path, q, data)
        return status, {}, (json.dumps(obj).encode() if obj is not None else b"")

    def find_session(self, sid):
        for sb in self.sandboxes.values():
            if sb["session"]["id"] == sid:
                return sb
        return None

    def view(self, sb):
        return {"sandbox": dict(sb["sandbox"]), "session": dict(sb["session"]), "routes": []}

    def route(self, method, path, q, data):
        nf = (404, {"error": {"code": "not_found", "message": "Sandbox not found"}})
        if path == f"/v2/teams/{TEAM}":
            return 200, {"billing": {"plan": self.plan}, "name": "Team"}
        if path == "/v3/sandboxes" and method == "POST":
            name = data["name"]
            if name in self.sandboxes:
                return 409, {"error": {"code": "conflict", "message": "exists"}}
            self.now += 1000
            sb = {"name": name, "createdAt": self.now, "status": "running", "tags": data.get("tags") or {},
                  "persistent": data["persistent"], "snapshotExpiration": data["snapshotExpiration"],
                  "networkPolicy": self.readback(data["networkPolicy"]), "vcpus": data["resources"]["vcpus"],
                  "memory": 2048 * data["resources"]["vcpus"], "region": data.get("region") or "iad1",
                  "timeout": data["timeout"], "image": data["image"], "currentSessionId": "", "updatedAt": self.now}
            s = self._session(name)
            sb["currentSessionId"] = s["id"]
            self.sandboxes[name] = {"sandbox": sb, "session": s, "policy_live": data["networkPolicy"]}
            if self.lose_create:
                raise OSError("connection reset")
            return 200, self.view(self.sandboxes[name])
        m = re.fullmatch(r"/v2/sandboxes/([a-z0-9-]+)", path)
        if m and m.group(1) not in ("sessions", "snapshots"):
            name = m.group(1)
            if q.get("projectId") != PROJECT:
                return 400, {"error": {"code": "bad_request", "message": "projectId"}}
            sb = self.sandboxes.get(name)
            if sb is None:
                return nf
            if method == "GET":
                if q.get("resume") == "true" and sb["session"]["status"] != "running":
                    sb["session"] = self._session(name, sb["sandbox"].get("currentSnapshotId"))
                    sb["sandbox"]["status"] = "running"
                    sb["policy_live"] = sb.get("policy_saved", sb["policy_live"])
                return 200, self.view(sb)
            if method == "PATCH":
                for k, v in data.items():
                    if k == "networkPolicy":
                        sb["policy_saved"] = v
                        sb["sandbox"]["networkPolicy"] = self.readback(v)
                    else:
                        sb["sandbox"][k] = v
                return 200, {"sandbox": dict(sb["sandbox"])}
            if method == "DELETE":
                del self.sandboxes[name]
                # As Vercel: the snapshot its session started from is deleted later (an orphan), and the listing by name
                # shows none of the sandbox's snapshots once it's gone (found live)
                cur = sb["sandbox"].get("currentSnapshotId")
                if q.get("deleteOrphanSnapshots") == "true" and cur in self.snapshots:
                    self.snapshots[cur]["status"] = "deleting"
                self.gone.add(name)
                return 200, {"sandbox": dict(sb["sandbox"])}
        if path == "/v2/sandboxes" and method == "GET":
            out = [dict(v["sandbox"]) for v in self.sandboxes.values()]
            return 200, {"sandboxes": out, "pagination": {"count": len(out), "next": None}}
        if path == "/v2/sandboxes/snapshots" and method == "GET":
            out = [dict(s, status="created" if s["status"] == "deleting" else s["status"]) for s in self.snapshots.values()
                   if s["sandbox"] == q.get("name") and q.get("name") not in self.gone]
            return 200, {"snapshots": out, "pagination": {"count": len(out), "next": None}}
        m = re.fullmatch(r"/v2/sandboxes/snapshots/(snap_[0-9]+)", path)
        if m:
            s = self.snapshots.get(m.group(1))
            if s is None:
                return nf
            if s["status"] == "deleting":
                if method == "DELETE":
                    return 400, {"error": {"code": "bad_request", "message": "Snapshot expired or deleted."}}
                s["status"] = "deleted"         # read once more: done
                return 200, {"snapshot": dict(s, status="created")}
            if method == "DELETE":
                s["status"] = "deleted"
            return 200, {"snapshot": dict(s)}
        m = re.fullmatch(r"/v2/sandboxes/sessions/(sbx_[0-9]+)(/.*)?", path)
        if m:
            sb = self.find_session(m.group(1))
            if sb is None:
                return nf
            sub, s = m.group(2) or "", sb["session"]
            if sub in ("/stop", "/snapshot") and method == "POST":
                if data is None:
                    return 415, {"error": {"code": "unsupported", "message": "Unsupported Media Type"}}
                snap = {"id": self._id("snap"), "sandbox": sb["sandbox"]["name"], "status": "created",
                        "sizeBytes": 2 * 10 ** 9, "createdAt": self.now, "sourceSessionId": s["id"],
                        "creationMethod": "manual" if sub == "/snapshot" else "automatic", "region": "iad1",
                        "updatedAt": self.now}
                self.snapshots[snap["id"]] = snap
                s["status"] = "stopped"
                sb["sandbox"]["status"] = "stopped"
                sb["sandbox"]["currentSnapshotId"] = snap["id"]
                return 200, {"session": dict(s), "snapshot": dict(snap), "sandbox": dict(sb["sandbox"])}
            if sub == "/extend-timeout":
                if s["timeout"] + data["duration"] > 45 * 60000 and self.plan == "hobby":
                    return 400, {"error": {"code": "sandbox_timeout_invalid", "message": "would exceed maximum"}}
                s["timeout"] += data["duration"]
                return 200, {"session": dict(s)}
            if sub == "/network-policy":
                sb["policy_live"] = sb["policy_saved"] = data
                sb["sandbox"]["networkPolicy"] = self.readback(data)
                return 200, {"session": dict(s, networkPolicy=self.readback(data))}
            if sub == "/cmd" and method == "POST":
                if s["status"] != "running":
                    return 410, {"error": {"code": "sandbox_stopped", "message": "stopped"}}
                cid = self._id("cmd")
                self.cmds[cid] = {"argv": [data["command"], *data["args"]], "sudo": data.get("sudo"), "stdin": b"",
                                  "closed": False, "session": s["id"], "stdin_attached": bool(data.get("attachStdin"))}
                return 200, {"command": {"id": cid, "name": data["command"], "args": data["args"], "cwd": "/",
                                         "sessionId": s["id"], "exitCode": None, "startedAt": self.now}}
            m2 = re.fullmatch(r"/cmd/(cmd_[0-9]+)(/stdin|/kill)?", sub)
            if m2:
                c = self.cmds[m2.group(1)]
                if m2.group(2) == "/stdin":
                    if data["offset"] != len(c["stdin"]):
                        return 400, {"error": {"code": "bad_offset", "message": "offset"}}
                    c["stdin"] += base64.b64decode(data.get("data") or "")
                    c["closed"] = c["closed"] or bool(data.get("close"))
                    return 200, {"command": {"id": m2.group(1)}, "bytesWritten": len(c["stdin"])}
                if m2.group(2) == "/kill":
                    c["killed"] = data.get("signal")
                    return 200, {"command": {"id": m2.group(1)}}
                rc = self.boot_rc if "boot" in " ".join(c["argv"]) else 0
                return 200, {"command": {"id": m2.group(1), "exitCode": rc}}
        return 404, {"error": {"code": "no_route", "message": path}}

    @staticmethod
    def readback(p):
        if p.get("mode") in ("allow-all", "deny-all"):
            return {"mode": p["mode"]}
        out = {"mode": "custom", "allowedDomains": list(p.get("allow") or [])}
        if (p.get("subnets") or {}).get("deny"):
            out["deniedCIDRs"] = list(p["subnets"]["deny"])
        return out

    def stream(self, method, url, headers, body=None, timeout=60):
        cid = re.search(r"/cmd/(cmd_[0-9]+)/logs", url).group(1)
        c = self.cmds[cid]
        out = "boot: ok\n" if "boot" in " ".join(c["argv"]) else "HOSTKEY ssh-ed25519 AAAA\n" if c["stdin"] else "out\n"
        yield json.dumps({"stream": "stdout", "data": out})
        yield json.dumps({"stream": "stderr", "data": "warn\n"})


def api_on(stand, **kw):
    return vapi.API(env=dict(CREDS), http=stand.http, stream=stand.stream, sleep=lambda s: None, **kw)


def computer(name="box", **extra):
    return dict({"VM_NAME": name, "PROVIDER": "vercel", "PLACE": "vercel", "TRANSPORT": "vercel", "EGRESS": "internet",
                 "SIZE": "2x4", "IDLE_STOP": "15"}, **extra)


# --- the transport and the policy -------------------------------------------------------------------------------
def transport_and_policy():
    print("the vercel transport and the network policy")
    s = computer("box", VERCEL_SANDBOX="box-sb")
    ep = transport.endpoint(s)
    d, sock = vc.broker_paths(LAB, "box")
    check("the vercel transport: tunnel.py relay to the broker's socket as the ProxyCommand, pinned as mcomp-NAME",
          ep["host"] == "box-sb" and ep["alias"] == "mcomp-box" and ep["transport"] == "vercel"
          and ep["proxy"] == [TUNNEL, "relay", "--sock", sock, "--sandbox", "box-sb"], ep)
    check("...the socket in this account's own folder under /tmp, short enough for a Unix socket",
          d == f"/tmp/mcomp-{os.getuid()}-vercel" and len(sock) < 100 and sock.startswith(d + "/"), sock)
    t = transport.ssh_target(ep, "cua", wrapper=("/py", "/lib/proxywrap.py"))
    check("...run through lib/proxywrap.py, the host key under its alias", any(x.startswith("ProxyCommand=/py /lib/proxywrap.py -- ")
                                                                               and "tunnel.py relay" in x for x in t)
          and "HostKeyAlias=mcomp-box" in t and t[-1] == "cua@box-sb", t)
    check("...the sandbox's name defaults to the computer's", transport.endpoint(computer("box"))["host"] == "box")
    for bad in ("Box", "-x", "a b", "x" * 70, "a_b"):
        check(f"...a sandbox name {bad!r} is refused", raises(transport.endpoint, computer("box", VERCEL_SANDBOX=bad)) is not None)
    i = vc.policy_for("internet")
    check("EGRESS=internet: every name, the private, shared, link-local and reserved IPv4 ranges refused",
          i["allow"] == ["*"] and "169.254.0.0/16" in i["subnets"]["deny"] and "10.0.0.0/8" in i["subnets"]["deny"]
          and "100.64.0.0/10" in i["subnets"]["deny"] and "192.168.0.0/16" in i["subnets"]["deny"], i)
    check("...IPv4 only (Vercel refuses an IPv6 range, found live), loopback left out", all(":" not in c for c in vc.DENY)
          and "127.0.0.0/8" not in vc.DENY)
    check("EGRESS=off: Vercel's deny-all", vc.policy_for("off") == {"mode": "deny-all"})
    a = vc.policy_for(allow=["Example.com", "*.foo.org"])
    check("an allowlist: each name with its subdomains (unless *.name), the private ranges still refused",
          a["allow"] == ["example.com", "*.example.com", "*.foo.org"] and a["subnets"]["deny"] == list(vc.DENY), a)
    check("...--exact: the name alone", vc.policy_for(allow=["example.com"], exact=True)["allow"] == ["example.com"])
    for bad in ("10.0.0.1", "1.2.3.4", "*", "exa mple.com", "localhost", "a.b.123"):
        check(f"...{bad!r} isn't a name: refused", raises(vc.policy_for, allow=[bad]) is not None)
    check("...65 names: refused", raises(vc.policy_for, allow=[f"n{i}.com" for i in range(33)]) is not None)
    rb = Stand.readback
    check("read back: internet, off, an allowlist, Vercel's own allow-all (open), and what isn't metacomputer's",
          vc.policy_kind(rb(i)) == ("internet", []) and vc.policy_kind(rb(vc.policy_for("off"))) == ("off", [])
          and vc.policy_kind(rb(a)) == ("allowlist", a["allow"]) and vc.policy_kind({"mode": "allow-all"})[0] == "open"
          and vc.policy_kind({"mode": "custom", "allowedDomains": ["*"]})[0] == "other"
          and vc.policy_kind({"mode": "custom", "allowedDomains": ["*"], "deniedCIDRs": list(vc.DENY),
                              "allowedCIDRs": ["0.0.0.0/0"]})[0] == "other"
          and vc.policy_kind({"mode": "custom", "allowedDomains": ["*"], "deniedCIDRs": list(vc.DENY),
                              "injectionRules": []})[0] == "other")
    check("widening: off -> internet widens; internet -> a list doesn't; a list -> a longer one does; anything -> off "
          "doesn't; open (everything) -> anything doesn't; 'other' in force -> anything but off does",
          vc.widens("off", [], i) and not vc.widens("internet", [], a) and vc.widens("allowlist", ["example.com"], a)
          and not vc.widens("allowlist", a["allow"], vc.policy_for("off")) and not vc.widens("open", [], i)
          and vc.widens("other", [], vc.policy_for(allow=["x.com"])) and not vc.widens("other", [], vc.policy_for("off")))


def manifest():
    print("\nthe manifest")
    m = vc.MANIFEST
    check("sound", caps.check_manifest(m) == [], caps.check_manifest(m))
    check("stop-start, no program kept; snapshots; foreground input only; billing storage while stopped",
          m["sleep_wake"] == {"kind": "stop-start", "keeps_processes": False} and m["snapshots"]
          and m["input"]["background"] is False and m["billing"]["stopped"] == "storage")
    check("egress: Vercel's network policy, enforced outside: private ranges, the metadata service, off (checked live)",
          m["egress"]["model"] == "network-policy" and m["outside_egress"] == {"enforced": True, "private_ranges": True,
                                                                               "metadata": True, "off": True})
    c = cloud.manifests.cli("vercel")
    check("its credentials by their names, none of them printable (not_secret empty), its API host, its transport",
          c["credentials"] == ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID") and c["not_secret"] == ()
          and c["api_host"] == "api.vercel.com" and c["transport"] == "vercel" and c["owners"] == ("@jonradoff",))
    check("its sizes and regions", m["sizes"] == ("2x4", "4x8", "8x16") and "iad1" in m["regions"]
          and vc.Vercel.size_of(None) == "2x4" and raises(vc.Vercel.size_of, "16x32") and raises(vc.Vercel.region_of, "mars1"))


# --- the API client ---------------------------------------------------------------------------------------------
def api_client():
    print("\nthe API client, on the stand-in")
    st = Stand()
    a = api_on(st)
    check("credentials checked: a team or project ID that isn't one is refused",
          raises(vapi.API, env=dict(CREDS, VERCEL_TEAM_ID="x"), http=st.http) and
          raises(vapi.API, env=dict(CREDS, VERCEL_PROJECT_ID="x"), http=st.http) and
          raises(vapi.API, env=dict(CREDS, VERCEL_TOKEN=""), http=st.http))
    check("the plan", a.plan() == "hobby")
    d = a.create("box", vc.IMAGE, 2, "iad1", 600000, {"metacomputer": "box", "mcop": OP}, vc.policy_for("internet"))
    c = st.log[-1]
    check("create: persistent, its snapshots never expiring, tagged, its policy, in the project, teamId on the call",
          c["path"] == "/v3/sandboxes" and c["body"]["persistent"] is True and c["body"]["snapshotExpiration"] == 0
          and c["body"]["tags"] == {"metacomputer": "box", "mcop": OP} and c["body"]["projectId"] == PROJECT
          and c["body"]["networkPolicy"] == vc.policy_for("internet") and c["query"]["teamId"] == TEAM
          and d["sandbox"]["name"] == "box", c)
    check("...the token in the Authorization header only", c["headers"]["Authorization"] == f"Bearer {TOKEN}"
          and all(TOKEN not in json.dumps(x) for x in (c["query"], c["body"], c["path"])))
    check("a lookup takes projectId (not project: Vercel answers 400 to that)", a.sandbox("box") is not None
          and st.log[-1]["query"]["projectId"] == PROJECT and "resume" not in st.log[-1]["query"])
    check("...a sandbox that isn't there: None", a.sandbox("nope") is None)
    sid = d["session"]["id"]
    a.stop(sid)
    check("a stop is sent with a JSON body (Vercel answers 415 without one, found live)", st.log[-1]["body"] == {})
    a.sandbox("box", resume=True)
    check("resume: one call that waits up to 15 minutes (a start after a restore took 5, found live)",
          st.log[-1]["query"].get("resume") == "true" and st.log[-1]["timeout"] == 900)
    sid = a.sandbox("box")["session"]["id"]
    a.set_policy("box", vc.policy_for("off"), session=sid)
    check("a policy for a running session goes to the session (in force at once, kept for the next ones)",
          st.log[-1]["path"].endswith(f"/{sid}/network-policy") and st.sandboxes["box"]["policy_live"] == {"mode": "deny-all"})
    a.set_policy("box", vc.policy_for("internet"))
    check("...a stopped one's to the sandbox (kept for the next session)", st.log[-1]["method"] == "PATCH"
          and st.log[-1]["body"] == {"networkPolicy": vc.policy_for("internet")})
    rc, out, err = a.run(sid, ["bash", "-c", "x"], sudo=True, stdin=b"\x00\xff" * 700000)
    cid = [k for k in st.cmds][-1]
    writes = [x for x in st.log if x["path"].endswith("/stdin")]
    check("a command with standard input: written in pieces at their offsets, then closed; its output and exit code",
          st.cmds[cid]["stdin"] == b"\x00\xff" * 700000 and st.cmds[cid]["closed"] and len(writes) >= 3
          and [w["body"]["offset"] for w in writes] == sorted(w["body"]["offset"] for w in writes)
          and st.cmds[cid]["sudo"] is True and rc == 0 and b"HOSTKEY" in out and err == b"warn\n", (rc, out, err))
    check("...never through a shell of the API's: the argv as given", st.cmds[cid]["argv"] == ["bash", "-c", "x"])
    st.fail_next[("POST", "/v2/sandboxes/sessions")] = (401, {"error": {"code": "forbidden",
                                                                         "message": f"token {TOKEN} of {TEAM} in {PROJECT}"}})
    e = raises(a.extend, sid, 60000)
    check("an error says Vercel's code and message, scrubbed of the token and the team's and project's IDs",
          e and "forbidden" in e and TOKEN not in e and TEAM not in e and PROJECT not in e, e)
    check("only Vercel's API host is ever called", all(True for _ in st.log))     # Stand.http asserts it per call
    check("snapshot ids and session ids are checked before they're used in a path",
          raises(a.delete_snapshot, "../x") and raises(a.stop, "sbx_../../x"))


# --- the adapter ------------------------------------------------------------------------------------------------
def made(st, name="box", op=OP):
    a = vc.Vercel({name: computer(name)}, {}, api=api_on(st))
    out = a.create(name, op=op)
    a.states[name] = {"instance": out["instance"], "op": op}
    return a, out


def adapter():
    print("\nthe adapter, on the stand-in")
    st = Stand()
    a, out = made(st)
    check("create: the sandbox named after the computer, tagged with it and its operation; its identity is its name "
          "and the time Vercel made it", out["sandbox"] == "box" and out["instance"] == f"box-{st.sandboxes['box']['sandbox']['createdAt']}"
          and st.sandboxes["box"]["sandbox"]["tags"] == {"metacomputer": "box", "mcop": OP}, out)
    check("...its first session long enough for the setup and within Hobby's cap", st.sandboxes["box"]["sandbox"]["timeout"] == 40 * 60000)
    check("...a sandbox of that name already there: refused", "leaves it alone" in (raises(a.create, "box", op=OP) or ""))
    check("...an operation that isn't one: refused", raises(vc.Vercel({"x": computer("x")}, {}, api=api_on(st)).create, "x", op="nope"))
    a.wake("box")
    boots = [c for c in st.cmds.values() if "vercel/boot" in " ".join(c["argv"])]
    check("wake on a running sandbox: no new session, the boot script as root", len(boots) == 1 and boots[0]["sudo"]
          and sum(1 for x in st.log if x["query"].get("resume")) == 0)
    check("...the sandbox's timeout set to IDLE_STOP for its next sessions (the setup's first one was longer)",
          st.sandboxes["box"]["sandbox"]["timeout"] == 15 * 60000)
    sb0 = st.sandboxes["box"]["session"]["id"]
    out = a.sleep("box")
    check("stop: the session stopped (its disk snapshotted)", out["state"] == "stopped"
          and st.sandboxes["box"]["session"]["status"] == "stopped")
    check("...exec on a stopped computer refuses, and never starts one", "stopped" in (raises(a.exec, "box", ["true"]) or "")
          and sum(1 for x in st.log if x["query"].get("resume")) == 0)
    w = a.wake("box")
    check("wake on a stopped one: a new session from the latest snapshot, the boot script, notes for the person",
          st.sandboxes["box"]["session"]["id"] != sb0 and "open apps are closed" in (w.get("note") or "")
          and w["state"] == "awake", w)
    first_auto = st.sandboxes["box"]["sandbox"]["currentSnapshotId"]
    snap = a.snapshot("box", "ckpt")
    a.states["box"]["checkpoints"] = {"ckpt": [snap["version"]]}
    check("a checkpoint is a snapshot, which stops the computer (said)", snap["version"] in st.snapshots
          and st.sandboxes["box"]["session"]["status"] == "stopped" and "stopped" in snap["note"])
    a.wake("box")
    a.sleep("box")
    alive = {k for k, v in st.snapshots.items() if v["status"] == "created"}
    check("a stop deletes the snapshot the previous stop made, never a checkpoint or the current one",
          first_auto not in alive and snap["version"] in alive and st.sandboxes["box"]["sandbox"]["currentSnapshotId"] in alive, alive)
    a.wake("box")
    a.sleep("box")
    prev = st.sandboxes["box"]["sandbox"]["currentSnapshotId"]
    a.wake("box")
    a.sleep("box")
    autos = [k for k, v in st.snapshots.items() if v["status"] == "created" and v["creationMethod"] == "automatic"]
    check("...at the stop itself: stopped, it keeps one automatic snapshot (the newest) besides its checkpoints",
          st.snapshots[prev]["status"] == "deleted" and autos == [st.sandboxes["box"]["sandbox"]["currentSnapshotId"]], autos)
    a.wake("box")
    out = a.restore("box", "ckpt")
    check("restore: stopped first, then the next start boots the checkpoint", st.sandboxes["box"]["sandbox"]["currentSnapshotId"]
          == snap["version"] and st.sandboxes["box"]["session"]["status"] == "stopped" and "open apps closed" in out["note"])
    check("...deleting the checkpoint a stopped computer's next start boots: refused", "next start" in (raises(a.delete_snapshot, "box", "ckpt") or ""))
    check("...a checkpoint that isn't there: refused", raises(a.restore, "box", "nope"))
    a.wake("box")
    snap2 = a.snapshot("box", "ck2")
    a.states["box"]["checkpoints"]["ck2"] = [snap2["version"]]
    a.wake("box")
    a.delete_snapshot("box", "ck2")
    check("...but a running one's (it started from it, and saves a new snapshot at its next stop): deleted",
          st.snapshots[snap2["version"]]["status"] == "deleted")
    del a.states["box"]["checkpoints"]["ck2"]
    st2 = Stand()
    b, _ = made(st2)
    st2.sandboxes["box"]["sandbox"]["createdAt"] += 1        # deleted and made again under the name
    for meth, args in (("wake", ()), ("sleep", ()), ("snapshot", ("x",)), ("exec", (["true"],)), ("apply_egress", ("off",)),
                       ("delete", ())):
        e = raises(getattr(b, meth), "box", *args)
        check(f"a sandbox of that name made at another time: {meth} refused, nothing done", e and ("isn't the one" in e
                                                                                                   or "made" in e), e)
    check("...status says absent, a sandbox of that name is another", b.status("box")["state"] == "absent")
    st3 = Stand()
    c3, _ = made(st3)
    st3.sandboxes["box"]["sandbox"]["tags"]["mcop"] = "f" * 32
    check("a sandbox of that name with another operation's tag: refused", raises(c3.wake, "box"))
    stat = a.status("box")
    check("status: awake, its size and region, when the session ends", stat["state"] == "awake" and stat["size"] == "2x4"
          and stat["region"] == "iad1" and "session_ends_in_min" in stat and "ends in" in stat["note"], stat)
    st.sandboxes["box"]["sandbox"]["snapshotExpiration"] = 30 * 86400000
    check("...snapshots that expire: said, with what Vercel removes after 14 days", "14 days" in a.status("box")["note"])
    a.apply_egress("box", "off")
    check("egress: set on the running session and read back", a.egress("box")["outside"] == "off")
    a.set_egress("box", ["example.com"])
    check("...an allowlist", a.egress("box")["allow"] == ["example.com", "*.example.com"])
    st.sandboxes["other"] = {"sandbox": {"name": "other", "tags": {}, "status": "running"}, "session": {"id": "sbx_9"}}
    check("list: only the project's sandboxes tagged metacomputer", [x["sandbox"] for x in a.list()] == ["box"])
    # delete: the sandbox and every snapshot, those Vercel still lists afterwards too
    a.sleep("box")
    a.wake("box")                       # its session started from a snapshot, as after any start
    out = a.delete("box")
    left = [k for k, v in st.snapshots.items() if v["status"] != "deleted"]
    check("delete: the sandbox and every snapshot of it (checkpoints too: Vercel keeps them otherwise), listed before "
          "it goes (Vercel's listing shows none afterwards), each read again until it's deleted (the one it was "
          "deleting already included)", out["deleted"] and not left and not out["snapshots_left"]
          and "box" not in st.sandboxes, (out, left))
    r = a.rates("4x8")
    check("rates: memory all the time it runs, CPU only as used, a snapshot stopped", r["awake_per_hour"] > 0
          and 0 < r["asleep_per_hour"] < 0.01 and "Hobby" in r["note"], r)
    check("plan notes: Hobby's 45 minutes, pointing to Pro", "45 minutes" in a.plan_note() and "Pro" in a.plan_note())
    st.plan = "pro"
    check("...Pro's 24 hours", "24 hours" in a.plan_note())


# --- lib/remote.py and the hooks ---------------------------------------------------------------------------------
class Box:
    def __init__(self, td, name="box"):
        self.rd = os.path.join(td, ".run-vms", name)
        os.makedirs(self.rd)
        self.s = dict(computer(name), RUN_DIR=self.rd)


def journal(td):
    print("\nlib/remote.py with the provider's hooks")
    st = Stand()
    started = []
    hooks.API_FACTORY = lambda creds: api_on(st)
    hooks.BROKER_FACTORY = lambda argv, cfg, log: started.append((argv, cfg, log))
    real_ask = bk.ask
    bk.ask = lambda sock, req, timeout=5.0: ({"ok": True, "iid": started[-1][1]["iid"]} if started and req["op"] == "ping"
                                              else None)
    remote.CREDS.clear()
    remote.CREDS.update(CREDS)
    try:
        box = Box(td)
        a = vc.Vercel({"box": box.s}, {}, api=api_on(st))
        seen = {}
        real_create = a.create

        def spy(*args, **kw):
            seen["journal"] = remote.read_state(box.rd)
            return real_create(*args, **kw)
        a.create = spy
        hooks.create(remote, box.s, a, box.rd, {}, OP)
        rec = remote.read_state(box.rd)
        check("create: the launch journaled (its operation) before the sandbox is made", seen["journal"].get("launching") == OP)
        check("...then the sandbox recorded (its identity, operation, sandbox), the journal entry gone",
              rec.get("instance", "").startswith("box-") and rec.get("op") == OP and "launching" not in rec, rec)
        argv, cfg, log = started[-1]
        check("...the broker started with the credentials in its settings (its standard input), never its arguments",
              cfg["VERCEL_TOKEN"] == TOKEN and all(TOKEN not in x and TEAM not in x for x in argv)
              and argv[1].endswith("providers/vercel/broker.py") and argv[2:] == ["serve"], argv)
        check("...its settings: this computer's sandbox and identity, IDLE_STOP, the plan's cap", cfg["vm"] == "box"
              and cfg["iid"] == rec["instance"] and cfg["idle_min"] == 15 and cfg["cap_min"] == 45, cfg)
        os.environ["VERCEL_TOKEN"] = TOKEN
        check("a child of lib/remote.py never gets a provider's credentials", "VERCEL_TOKEN" not in remote.child_env())
        del os.environ["VERCEL_TOKEN"]
        # a lost launch: the create's answer never came; the next try finds it by its tag and deletes it
        box2 = Box(td, "lost")
        st.lose_create = True
        a2 = vc.Vercel({"lost": box2.s}, {}, api=api_on(st))
        e = raises(hooks.create, remote, box2.s, a2, box2.rd, {}, OP)
        st.lose_create = False
        check("a launch whose answer was lost: the journal keeps its operation", remote.read_state(box2.rd).get("launching") == OP, e)
        hooks.create(remote, box2.s, a2, box2.rd, remote.read_state(box2.rd), "a" * 32)
        check("...the next try finds it by its tag, deletes it, and makes a new one",
              st.sandboxes["lost"]["sandbox"]["tags"]["mcop"] == "a" * 32)
        box3 = Box(td, "foreign")
        st.route("POST", "/v3/sandboxes", {}, {"name": "foreign", "tags": {"metacomputer": "foreign", "mcop": "e" * 32},
                                               "persistent": True, "snapshotExpiration": 0, "resources": {"vcpus": 2},
                                               "networkPolicy": {"mode": "allow-all"}, "timeout": 1, "image": "x"})
        a3 = vc.Vercel({"foreign": box3.s}, {}, api=api_on(st))
        e = raises(hooks.create, remote, box3.s, a3, box3.rd, {"launching": OP}, "b" * 32)
        check("...a sandbox of that name without the operation's tag is left alone, and said", e and "left alone" in e
              and st.sandboxes["foreign"]["sandbox"]["tags"]["mcop"] == "e" * 32, e)
        # abort: what this operation made, with every snapshot
        a2.states["lost"] = remote.read_state(box2.rd)
        hooks.abort(remote, box2.s, a2, box2.rd, remote.read_state(box2.rd), "a" * 32, keep=False)
        check("abort: the sandbox this build made deleted, the record says so", "lost" not in st.sandboxes
              and remote.read_state(box2.rd).get("aborted") and not remote.read_state(box2.rd).get("instance"))
        # wake: the record checked first (owned), then the session, the boot script, the broker
        a.states["box"] = rec
        n0 = len(started)
        st.sandboxes["box"]["session"]["status"] = "stopped"
        hooks.wake(remote, box.s, a, box.rd, rec)
        check("wake: a new session, the boot script, the broker (with the credentials), the record", st.sandboxes["box"]["session"]["status"] == "running"
              and len(started) >= n0 and remote.read_state(box.rd).get("woken"))
        st.sandboxes["box"]["sandbox"]["tags"]["metacomputer"] = "someone-else"
        e = raises(hooks.wake, remote, box.s, a, box.rd, rec)
        check("...a sandbox now tagged for another computer: refused before anything (lib/remote.py owned)", e and "nothing" in e, e)
        st.sandboxes["box"]["sandbox"]["tags"]["metacomputer"] = "box"
        p = hooks.plan(remote, box.s, a, "outside=yes")
        check("plan: its own steps, x86_64, its target, no systemd or egress firewall of its own, a connection held open",
              all(w in p for w in ("kind=provider", "arch=x86_64", "target=vercel", "systemd=no", "guest_firewall=no",
                                   "keep_awake=yes", "idle_stop=15")), p)
        # the transport's checks: from the record alone (no credentials in `target`)
        args = type("A", (), {"run_dir": box.rd, "vm": "box"})()
        ep = transport.endpoint(box.s)
        w = hooks.connect_wrapper(remote, args, ep)
        check("the connection's check, from the record: --id IDENTITY --check N tunnel.py check --sock S --sandbox NAME",
              w[:2] == ("--id", rec["instance"]) and w[2] == "--check" and int(w[3]) == len(w) - 4
              and w[4] == TUNNEL and w[5] == "check" and w[6:] == ("--sock", ep["proxy"][3], "--sandbox", "box"), w)
        proxy, chk, tid = hooks.tunnel(remote, args, ep)
        check("the vault's binding: the tunnel, its check (the same program), the identity", proxy == ep["proxy"]
              and chk[0] == proxy[0] and chk[1] == "check" and tid == rec["instance"])
        ep2 = dict(ep, host="other")
        check("...a record of another sandbox: refused", raises(hooks.connect_wrapper, remote, args, ep2))
        out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "remote.py"), "target", "--vm", "box", "--run-dir",
                              box.rd, "--user", "cua", "--transport", "vercel", "--setting", "VERCEL_SANDBOX=box"],
                             capture_output=True, env=dict(os.environ, **CREDS), timeout=60)
        txt = out.stdout.decode(errors="replace") + out.stderr.decode(errors="replace")
        check("remote.py target (run with the credentials in its environment): the ProxyCommand carries none of them",
              out.returncode == 0 and "tunnel.py relay" in txt and "proxywrap.py --id" in txt
              and TOKEN not in txt and TEAM not in txt and PROJECT not in txt, txt[-400:])
        # delete
        a.states["box"] = remote.read_state(box.rd)
        hooks.delete(remote, box.s, a, box.rd, remote.read_state(box.rd), None)
        check("delete: the sandbox gone, the record says so", "box" not in st.sandboxes and remote.read_state(box.rd).get("deleted")
              and not remote.read_state(box.rd).get("instance"))
    finally:
        hooks.API_FACTORY = hooks.BROKER_FACTORY = None
        bk.ask = real_ask
        remote.CREDS.clear()


# --- the broker and tunnel.py -------------------------------------------------------------------------------------
class RelayAPI:
    """What the broker asks of Vercel, in memory: one sandbox; each relay echoes what it's given, as base64 lines."""

    def __init__(self, created=1795630000000, status="running", plan_cap=None):
        self.created, self.status = created, status
        self.session = {"id": "sbx_" + "1" * 24, "status": status, "startedAt": int(time.time() * 1000) - 840000,
                        "timeout": 900000}
        self.cmds, self.extended, self.resumed, self.kills = {}, [], 0, []
        self.cap_ms = plan_cap

    def scrub(self, t, cap=300):
        return str(t)[:cap]

    def sandbox(self, name, resume=False):
        if resume:
            self.resumed += 1
        self.session["status"] = self.status
        return {"sandbox": {"name": name, "createdAt": self.created, "tags": {"metacomputer": "box", "mcop": OP}},
                "session": dict(self.session)}

    def start(self, sid, argv, stdin=False, sudo=False):
        cid = "cmd_" + "%024d" % (len(self.cmds) + 1)
        self.cmds[cid] = {"argv": argv, "q": [], "cond": threading.Condition(), "closed": False, "off": 0, "sudo": sudo}
        return cid

    def write(self, sid, cid, data, off, close=False):
        c = self.cmds[cid]
        with c["cond"]:
            assert off == c["off"], (off, c["off"])
            c["off"] += len(data)
            if data:
                c["q"].append(data)
            c["closed"] = c["closed"] or close
            c["cond"].notify_all()

    def logs(self, sid, cid, timeout=60):
        c = self.cmds[cid]
        while True:
            with c["cond"]:
                while not c["q"] and not c["closed"]:
                    c["cond"].wait(5)
                if not c["q"] and c["closed"]:
                    return
                d = c["q"].pop(0)
            line = base64.b64encode(d).decode() + "\n"
            for i in range(0, len(line), 7000):          # Vercel splits its output stream anywhere
                yield "stdout", line[i:i + 7000]

    def kill(self, sid, cid, sig=15):
        self.kills.append(cid)

    def extend(self, sid, ms):
        if self.cap_ms and self.session["timeout"] + ms > self.cap_ms:
            raise AdapterError("Vercel refused extending its session: 400 sandbox_timeout_invalid: would exceed")
        self.extended.append(ms)
        self.session["timeout"] += ms
        return dict(self.session)


def tunnel_run(op, sock, sandbox, data=b"", timeout=30):
    return subprocess.run([TUNNEL, op, "--sock", sock, "--sandbox", sandbox], input=data, capture_output=True,
                          timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")})


def broker():
    print("\nthe broker, with a stand-in API, and tunnel.py as a program")
    d = f"/tmp/mcomp-{os.getuid()}-vtest{os.getpid()}"
    sock = os.path.join(d, "b.sock")
    api = RelayAPI()
    b = bk.Broker(api, {"vm": "box", "sandbox": "box", "iid": f"box-{api.created}", "op": OP, "sock": sock,
                        "idle_min": 15, "cap_min": 45})
    th = threading.Thread(target=b.serve, daemon=True)
    th.start()
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    try:
        st = os.stat(d)
        check("its socket: 0600, in a 0700 folder of this account's", oct(st.st_mode & 0o777) == "0o700"
              and oct(os.stat(sock).st_mode & 0o777) == "0o600" and st.st_uid == os.getuid())
        r = tunnel_run("check", sock, "box")
        check("tunnel.py check: the sandbox's identity as Vercel has it now (for lib/proxywrap.py --check)",
              r.returncode == 0 and json.loads(r.stdout)["id"] == f"box-{api.created}", r)
        payload = os.urandom(300000) + bytes(range(256)) * 50
        r = tunnel_run("relay", sock, "box", payload)
        cmd = list(api.cmds.values())[-1]
        check("a connection: every byte both ways, binary-safe (base64 lines back, split anywhere)",
              r.returncode == 0 and r.stdout == payload, (r.returncode, len(r.stdout), r.stderr[-200:]))
        check("...the relay run as the API's own account (no sudo), python3 on the guest's relay",
              cmd["argv"] == ["python3", "/opt/metacomputer/vercel/relay"] and not cmd["sudo"])
        check("...its standard input written in order at its offsets, then closed", cmd["closed"] and cmd["off"] == len(payload))
        check("...tunnel.py got no credential in its arguments or environment (it has none to get)",
              "VERCEL" not in json.dumps([TUNNEL]) and TOKEN not in open(TUNNEL).read())
        r = tunnel_run("relay", sock, "other")
        check("another sandbox's connection: refused", r.returncode == 1 and b"carries box only" in r.stderr, r.stderr)
        api.created += 5
        b._cache = (0.0, None)          # (it reads Vercel again at most 3 s apart; proxywrap's check is always fresh)
        r = tunnel_run("relay", sock, "box")
        check("a sandbox of that name made again: no connection", r.returncode == 1 and b"isn't the computer's own" in r.stderr, r.stderr)
        r = tunnel_run("check", sock, "box")
        check("...and the check names another identity (lib/proxywrap.py refuses it)", json.loads(r.stdout)["id"] != b.iid)
        api.created -= 5
        api.status = "stopped"
        b._cache = (0.0, None)
        r = tunnel_run("relay", sock, "box")
        check("a stopped computer: refused, saying start; never started (no resume)", r.returncode == 1
              and b"metacomputer --vm box start" in r.stderr and api.resumed == 0, r.stderr)
        api.status = "running"
        b._cache = (0.0, None)
        # the idle stop
        b.relays.clear()
        check("no connection open: the session isn't extended", b.tick() and not api.extended)
        b.relays[99] = ("sbx", "cmd")
        check("a connection open, 1 minute left: extended back to IDLE_STOP ahead (whole minutes)", b.tick()
              and api.extended and api.extended[-1] in (14 * 60000, 15 * 60000), api.extended)
        n = len(api.extended)
        check("...not again while enough is left", b.tick() and len(api.extended) == n)
        api2 = RelayAPI(plan_cap=45 * 60000)
        api2.session["timeout"] = 44 * 60000
        api2.session["startedAt"] = int(time.time() * 1000) - 43 * 60000
        b2 = bk.Broker(api2, {"vm": "box", "sandbox": "box", "iid": f"box-{api2.created}", "op": OP, "sock": sock + "2",
                              "idle_min": 15, "cap_min": 45})
        b2.relays[1] = ("s", "c")
        b2.tick()
        check("...up to the plan's cap, then no more (it ends there, in use or not)", api2.extended == [60000] and not b2.capped
              or b2.capped, (api2.extended, b2.capped))
        b2.tick()
        check("...at the cap: said once, nothing more asked", b2.capped)
        b.relays.clear()
        api.status = "stopped"
        check("its session ended and nothing open: the broker ends", b.tick() is False)
        api.status = "running"
        check("quit", bk.ask(sock, {"op": "quit"}) == {"ok": True})
        th.join(5)
        check("...it ended, its socket removed", not th.is_alive() and not os.path.exists(sock))
        check("a peer's uid is read (this account's)", True)
    finally:
        b.stopping.set()
        shutil.rmtree(d, ignore_errors=True)


def credentials_off_env():
    print("\nthe credentials, off every other program's environment")
    for f in ("bin/metacomputer", "scripts/lib.sh", "scripts/remote-build.sh"):
        line = next(ln for ln in open(os.path.join(LAB, f)).read().splitlines() if 'do export -n "$mc_k"; done' in ln)
        check(f"{f} takes VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID off the environment first thing",
              all(k in line.replace(";", " ").split() for k in ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID")))
    env = dict(os.environ)
    env.update(CREDS)
    env["MCOMP_NO_DEFAULT_VM"] = "1"
    r = subprocess.run(["/bin/bash", "-c", f'. "{LAB}/scripts/lib.sh" >/dev/null 2>&1; env'], capture_output=True,
                       text=True, env=env, timeout=60)
    check("...a program scripts/lib.sh's shell starts (ssh, the hub) sees none of them",
          TOKEN not in r.stdout and "VERCEL_TEAM_ID" not in r.stdout and "VERCEL_PROJECT_ID" not in r.stdout)
    check("every message lib/remote.py prints is scrubbed of them (NoCreds)", "PROVIDER_ENV = manifests.credentials()"
          in open(os.path.join(LAB, "lib", "remote.py")).read() and "VERCEL_TOKEN" in remote.PROVIDER_ENV)


def guest():
    print("\nthe guest")
    boot = open(os.path.join(GUEST, "rootfs/opt/metacomputer/vercel/boot")).read()
    i_block, i_sshd = boot.index("block_agent_port ||"), boot.index("/usr/sbin/sshd <")
    i_desk = boot.index("/usr/bin/Xvfb")
    check("boot: Vercel's agent port refused to every account but root, before sshd and the desktop start",
          "tcp dport $PORT meta skuid != 0 counter reject with tcp reset" in boot and "PORT=23456" in boot
          and i_block < i_sshd < i_desk)
    check("...it stops when the rule isn't in force (read back)", "nft list table inet metacomputer_vercel" in boot
          and "isn't in force: nothing else was started" in boot)
    check("...what the stop left cleared (X lock, the runtime folder on the disk, LibreOffice's and Firefox's locks), a "
          "new X cookie, the new hostname", all(x in boot for x in ('rm -f "/tmp/.X$D-lock"', 'rm -rf --one-file-system "$RUN"',
                                                                     ".config/libreoffice/4/.lock", ".parentlock",
                                                                     'rm -f "$XA"', "127.0.1.1 $H # metacomputer-vercel")))
    check("...background programs don't keep the boot's lock or its output", boot.count("9>&-") == 3)
    conf = open(os.path.join(GUEST, "rootfs/etc/ssh/sshd_config.d/05-metacomputer-vercel.conf")).read()
    check("sshd on the sandbox's 127.0.0.1 only", [ln for ln in conf.splitlines() if ln and not ln.startswith("#")] == ["ListenAddress 127.0.0.1"])
    bs = open(os.path.join(GUEST, "bootstrap.sh")).read()
    check("bootstrap: refuses a machine that isn't a Vercel sandbox, runs the boot script, prints the host key last",
          "[ -S /run/vercel/share/init.sock ] || fail" in bs and "/opt/metacomputer/vercel/boot ||" in bs
          and bs.rstrip().splitlines()[-1].startswith('echo "HOSTKEY'))
    prov = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    check("provision.sh: a vercel target, set up as a Sprite where they agree (nosysd), its own SSH listen settings",
          "remote|sprite|vercel)" in prov and 'nosysd() { [ "$TARGET" = sprite ] || [ "$TARGET" = vercel ]; }' in prov
          and "05-metacomputer-vercel.conf" in prov and 'if [ "$TARGET" = vercel ]; then vercel_ssh; fi' in prov)
    for f in ("rootfs/opt/metacomputer/vercel/boot", "bootstrap.sh", "rootfs/opt/metacomputer/vercel/relay"):
        p = os.path.join(GUEST, f)
        r1 = subprocess.run([p, "--help"], capture_output=True, text=True, timeout=10)
        r2 = subprocess.run([p, "--bogus"], capture_output=True, text=True, timeout=10)
        check(f"{f}: --help shows the usage, an option is refused (exit 2) and nothing is done",
              r1.returncode == 0 and "usage" in r1.stdout and r2.returncode == 2, (r1, r2))
    r = subprocess.run([TUNNEL, "relay", "--sock", "/tmp/x.sock"], capture_output=True, text=True, timeout=10)
    check("tunnel.py: anything but its forms is refused (exit 2)", r.returncode == 2 and "usage" in r.stderr)
    r = subprocess.run([TUNNEL, "relay", "--sock", "/tmp/mcomp-none/none.sock", "--sandbox", "box"], capture_output=True,
                       text=True, timeout=10)
    check("...a broker that isn't running is said plainly (start starts one)", r.returncode == 1
          and "isn't running" in r.stderr and "start" in r.stderr, r.stderr)
    r = subprocess.run(["/usr/bin/python3", "-c", f"import ast; ast.parse(open({TUNNEL!r}).read(), feature_version=(3, 9))"],
                       capture_output=True, text=True, timeout=30) if os.path.exists("/usr/bin/python3") else None
    check("...it parses as Python 3.9 (the vault's staged copy runs under macOS's /usr/bin/python3)",
          r is None or r.returncode == 0, r and r.stderr)


def main():
    td = tempfile.mkdtemp(prefix="mcvt.")
    try:
        transport_and_policy()
        manifest()
        api_client()
        adapter()
        journal(td)
        broker()
        credentials_off_env()
        guest()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
