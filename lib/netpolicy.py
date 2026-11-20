#!/usr/bin/env python3
"""Per-task network allowlists for the VM (`metacomputer net ...`), enforced by the gateway on this Mac.

  net status [--json]                      what the VM can reach now
  net allow NAME... [--for D] [--exact]
                                           with no list in force: start one (anyone, Claude too), for D
                                           (default 2h, at most 24h). With one in force: add names or time
                                           (your own terminal only: that widens it)
  net remove NAME...                       take names off the list in force (anyone: that narrows it)
  net clear                                lift the list before it ends (your own terminal only)

While a list is in force the VM reaches only the names on it (NAME also covers its subdomains unless
--exact; `*.example.com` covers only the subdomains): the gateway answers DNS only for those names and
lets the guest connect only to the addresses its own resolver returned for them, for as long as each
answer lives. Everything else is refused and logged, as other blocked connections are. The list ends
by itself when its time is up (a small timer on this Mac lifts it); nothing in the VM can change it.

Files (this VM's RUN_DIR, never in the checkout's config): net-allow.json (the list), netgw-rules.json
(the rules the gateway reads: config/netgw-rules.json plus the list; the gateway reloads it within a
second and closes connections the new rules refuse). Every change happens under one lock: the list is
saved first, then the rules file is written (checked with the gateway's own -check-rules before it
replaces the old one), then, while the VM runs, the gateway is asked which rules it has in force (a
digest over its control socket, on this Mac) until it's the file just written. So an interrupted change
never leaves the saved list wider than the gateway's rules, and "in force" is the gateway's word, not
the VM's.

Internal: `net render` (start writes the rules file), `net _expire TS` (the timer).
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_RULES = os.path.join(LAB, "config", "netgw-rules.json")
DEFAULT_FOR_S = 2 * 3600
MAX_FOR_S = 24 * 3600
MIN_FOR_S = 60
MAX_NAMES = 64                     # patterns in one list (a name without --exact counts twice)
REASON = "lab:allowlist"
STATE_VERSION = 1
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CONFIRM_S = 4.0                    # how long the gateway gets to load a new rules file (it looks every 250 ms)


class Refused(Exception):
    """A request this command won't carry out; exit 3 with the message."""


def run_dir():
    d = os.environ.get("MCOMP_RUN_DIR") or ""
    if not d or not os.path.isabs(d):
        raise Refused("no run folder for this VM (run this through `metacomputer net`)")
    return d


def paths():
    d = run_dir()
    return {"state": os.path.join(d, "net-allow.json"), "rules": os.path.join(d, "netgw-rules.json"),
            "lock": os.path.join(d, "net-allow.lock"), "gw": os.path.join(d, "gw.json"),
            "timer": os.path.join(d, "net-expiry.pid")}


class Lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        os.close(self.fd)


def normalize(name, exact=False):
    """The gateway patterns for one name, or Refused. Lowercase letters, digits and hyphens, at least two labels;
    `*.x.y` for only the subdomains. A plain name covers itself and its subdomains unless exact."""
    if not isinstance(name, str):
        raise Refused("a name must be text")
    n = name.strip().lower().rstrip(".")
    wild = n.startswith("*.")
    core = n[2:] if wild else n
    labels = core.split(".")
    if (len(core) > 253 or len(labels) < 2 or not all(LABEL_RE.match(lb) for lb in labels)
            or all(lb.isdigit() for lb in labels)):
        raise Refused(f"not a host name: {name[:80]!r} (letters, digits and hyphens, like example.com or *.example.com;"
                      " addresses aren't accepted)")
    if wild:
        return [n]
    return [core] if exact else [core, "*." + core]


def covered(pattern, allow):
    """Whether pattern is already reachable under the patterns in allow (so adding it widens nothing)."""
    if pattern in allow:
        return True
    p = pattern[2:] if pattern.startswith("*.") else pattern
    for a in allow:
        if a.startswith("*.") and p.endswith("." + a[2:]):
            return True                   # a subdomain (or a wildcard below one) of an allowed wildcard
    return False


def parse_for(text):
    m = re.fullmatch(r"\s*(\d{1,5})\s*([smhd]?)\s*", text or "")
    if not m:
        raise Refused(f"not a duration: {text!r} (like 30m, 2h or 1d)")
    n = int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if n < MIN_FOR_S or n > MAX_FOR_S:
        raise Refused("a list lasts from 1 minute to 24 hours")
    return n


def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as f:
        return json.load(f)


def read_state(p, now=None):
    """The list in force, or None. A list whose time is up counts as none (the timer or the next command removes it).
    A file that can't be read or doesn't have the expected shape is an error (Refused): never treated as no list."""
    try:
        st = read_json(p["state"])
    except (OSError, ValueError) as e:
        raise Refused(f"can't read {p['state']} ({type(e).__name__}); fix or remove it at your own terminal") from None
    if st is None:
        return None
    if not (isinstance(st, dict) and st.get("version") == STATE_VERSION and isinstance(st.get("allow"), list)
            and all(isinstance(a, str) for a in st["allow"]) and isinstance(st.get("expires"), (int, float))
            and isinstance(st.get("started"), (int, float))):
        raise Refused(f"{p['state']} isn't an allowlist this version reads; remove it at your own terminal")
    for a in st["allow"]:
        normalize(a, exact=True)          # every stored pattern must still be one the gateway accepts
    if (now or time.time()) >= st["expires"]:
        return None
    return st


def write_private(path, text):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".net-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        return tmp
    except BaseException:
        os.unlink(tmp)
        raise


def base_rules():
    try:
        with open(BASE_RULES) as f:
            base = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"can't read {BASE_RULES} ({type(e).__name__})") from None
    if not isinstance(base, dict) or not isinstance(base.get("rules"), list):
        raise Refused(f"{BASE_RULES} isn't a rules file")
    return base


def compose(base, st):
    """The gateway's rules: the base file, plus the list in force. With a list, nothing the base doesn't name is
    reachable by default, and only the listed names are answered and opened."""
    if st is None:
        return base
    if base.get("default_accept") is not True or base.get("domains") or any(
            isinstance(r, dict) and r.get("accept") is not False for r in base["rules"]):
        raise Refused(f"{BASE_RULES} already opens or restricts addresses or names of its own; an allowlist only goes "
                      "on top of rules that refuse ranges and accept the rest by default")
    out = dict(base)
    out["default_accept"] = False
    out["default_reason"] = REASON
    out["domains"] = {"allow": sorted(set(st["allow"]))}
    return out


def check_rules(path):
    gw = os.environ.get("MCOMP_NETGW_BIN") or ""
    if not gw or not os.access(gw, os.X_OK):
        raise Refused("the gateway (mcomp-netgw) isn't installed here, so the rules can't be checked; nothing changed")
    r = subprocess.run([gw, "-check-rules", path], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise Refused("the gateway refused the rules ({}); nothing changed".format(
            (r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout).strip() else r.returncode))


def render(p, st):
    """Write the rules file the gateway reads, checked first; the old file stays until the new one passes. Returns
    the sha256 of what was written (the gateway's control socket reports the same for the file it loaded)."""
    text = json.dumps(compose(base_rules(), st), indent=2) + "\n"
    tmp = write_private(p["rules"], text)
    try:
        check_rules(tmp)
        os.replace(tmp, p["rules"])
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return hashlib.sha256(text.encode()).hexdigest()


def gateway_digest():
    """The sha256 of the rules the running gateway has in force (mcomp-netgw's control socket, `rules`), "old" for a
    gateway that doesn't report it (cua-netgw before the patch), or None when it can't be asked."""
    path = os.environ.get("MCOMP_NETGW_CONTROL") or ""
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX) as c:
            c.settimeout(3)
            c.connect(path)
            c.sendall(b"rules\n")
            reply = c.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return None
    m = re.fullmatch(r"ok ([0-9a-f]{64})", reply)
    if m:
        return m.group(1)
    return "old" if reply.startswith("no unknown request") else None


def confirm(p, digest):
    """Whether the running gateway has the rules file just written in force: True, False with why, or None (the VM
    isn't running: the file is read when it starts)."""
    if not gateway_running():
        return None, ""
    if gateway_reads_it(p) is not True:
        return False, "the running gateway was started by an earlier version and reads other rules"
    deadline = time.monotonic() + CONFIRM_S
    while True:
        have = gateway_digest()
        if have == digest:
            return True, ""
        if have == "old":
            return False, "the running gateway is an earlier build that can't take an allowlist"
        if time.monotonic() >= deadline:
            return False, ("the gateway didn't take the new rules (see `metacomputer logs`)" if have else
                           "the gateway's control socket didn't answer")
        time.sleep(0.25)


def gateway_reads_it(p):
    """Whether this VM's running gateway reads the rules file in RUN_DIR (a gateway started by an earlier version
    reads config/netgw-rules.json and would never see a list). None when no gateway config is there."""
    try:
        gw = read_json(p["gw"])
    except (OSError, ValueError):
        return False
    if gw is None:
        return None
    return isinstance(gw, dict) and gw.get("rules") == p["rules"]


def gateway_running():
    return os.environ.get("MCOMP_NETGW_ALIVE") == "1"


def emit(type_, message, data, severity=None):
    """A security event (docs/events.md): counts and minutes only, never the names. Best effort, never fails."""
    try:
        import events
        events.emit("security", type_, message, severity=severity, data=data, origin="cli")
    except Exception:
        pass


def start_timer(p, expires):
    """The detached timer that lifts this list when it ends (one per VM: a newer one replaces the older)."""
    stop_timer(p)
    r = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_expire", str(int(expires))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env=dict(os.environ))
    tmp = write_private(p["timer"], f"{r.pid}\n")
    os.replace(tmp, p["timer"])


def stop_timer(p):
    try:
        with open(p["timer"]) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
            if "netpolicy.py _expire" in out:
                os.kill(pid, 15)
        except OSError:
            pass
    try:
        os.unlink(p["timer"])
    except OSError:
        pass


def until(ts):
    return time.strftime("%H:%M", time.localtime(ts)) + (
        "" if time.localtime(ts).tm_yday == time.localtime().tm_yday else time.strftime(" on %b %d", time.localtime(ts)))


def at_terminal():
    return sys.stdin.isatty() and sys.stderr.isatty()


def describe(st):
    if st is None:
        return "open: the public internet (config/netgw-rules.json's refusals still apply)"
    shown = sorted({a[2:] if a.startswith("*.") and a[2:] in st["allow"] else a for a in st["allow"]})
    return f"allowlist until {until(st['expires'])}: " + ", ".join(shown)


# --- commands ---------------------------------------------------------------------------------------------------
def expected_digest(st):
    """The sha256 of the rules file the saved list (st, or None) makes: what render() writes for it."""
    return hashlib.sha256((json.dumps(compose(base_rules(), st), indent=2) + "\n").encode()).hexdigest()


def in_force(p, st):
    """Whether the running gateway has in force the rules the saved list makes (not merely the file on disk, which an
    interrupted change can leave behind): True, False, or None (not running, or it can't be asked)."""
    if not gateway_running():
        return None
    if gateway_reads_it(p) is not True:
        return False
    try:
        want = expected_digest(st)
    except Refused:
        return False
    have = gateway_digest()
    return None if have is None else have == want


def cmd_status(a, p):
    with Lock(p["lock"]):                 # not halfway through a change
        st = read_state(p)
        reads = gateway_reads_it(p)
        force = in_force(p, st)
    if a.json:
        print(json.dumps({"allowlist": None if st is None else {"allow": sorted(st["allow"]), "expires": st["expires"],
                                                                "started": st["started"]},
                          "gateway_reads_list": reads, "gateway_running": gateway_running(), "in_force": force}))
        return 0
    print("network       " + describe(st))
    if st is not None and force is False:
        print("              not in force: the running gateway doesn't have these rules (an earlier version, or it "
              "couldn't load them): `metacomputer stop` and `start`")
    return 0


def report(p, digest, what):
    """After a change (rendered under the lock, digest of what was written): say whether it's in force. Exit code."""
    ok, why = confirm(p, digest)
    if ok is None:
        print(f"{what}; the VM isn't running, so it takes effect when it starts")
        return 0
    if ok:
        print(f"{what} (in force: the gateway reports these rules)")
        return 0
    print(f"{what}, but {why}: `metacomputer stop` and `start` to put it in force. The saved list stays and "
          "applies at the next start", file=sys.stderr)
    return 4


def commit(p, new):
    """Save the list (or remove it: new None), then write the rules for it. The list goes first, so an interruption
    leaves the saved list no wider than the gateway's rules: the next render applies the saved one."""
    if new is None:
        try:
            os.unlink(p["state"])
        except FileNotFoundError:
            pass
    else:
        os.replace(write_private(p["state"], json.dumps(new) + "\n"), p["state"])
    try:
        return render(p, new)
    except Refused as e:
        raise Refused(f"{e}. The change to the list was saved: it applies at the next start, or once the rules can "
                      "be written (metacomputer net status)") from None


def cmd_allow(a, p):
    pats = []
    for n in a.names:
        pats += normalize(n, a.exact)
    with Lock(p["lock"]):
        now = time.time()
        st = read_state(p, now)
        if st is None:
            secs = parse_for(a.for_) if a.for_ else DEFAULT_FOR_S
            allow = sorted(set(pats))
            if len(allow) > MAX_NAMES:
                raise Refused(f"at most {MAX_NAMES} patterns in one list (a name without --exact counts twice)")
            new = {"version": STATE_VERSION, "allow": allow, "started": int(now), "expires": int(now + secs)}
            digest = commit(p, new)
            start_timer(p, new["expires"])
            rc = report(p, digest, "network: " + describe(new))
            emit("net_allowlist_started", f"network allowlist started: {len(allow)} names until {until(new['expires'])}",
                 {"names": len(allow), "minutes": secs // 60}, severity="info")
            return rc
        wider = [x for x in pats if not covered(x, st["allow"])]
        longer = a.for_ and now + parse_for(a.for_) > st["expires"]
        if not wider and not longer:
            digest = render(p, st)            # the same rules again: is the gateway enforcing them?
            return report(p, digest, "network: already allowed; nothing changed (" + describe(st) + ")")
        if not at_terminal():
            raise Refused("an allowlist is in force, and adding names or time widens it, which only you can do, at your "
                          "own terminal: metacomputer net allow " + " ".join(a.names) +
                          (f" --for {a.for_}" if a.for_ else "") + ". Nothing changed")
        allow = sorted(set(st["allow"]) | set(wider))
        if len(allow) > MAX_NAMES:
            raise Refused(f"at most {MAX_NAMES} patterns in one list")
        new = dict(st, allow=allow)
        if longer:
            new["expires"] = int(now + parse_for(a.for_))
        digest = commit(p, new)
        if longer:
            start_timer(p, new["expires"])
        rc = report(p, digest, "network: " + describe(new))
        emit("net_allowlist_changed", f"network allowlist widened at the terminal: {len(allow)} names until "
             f"{until(new['expires'])}", {"names": len(allow), "added": len(wider), "longer": bool(longer)})
        return rc


def cmd_remove(a, p):
    gone = set()
    for n in a.names:
        gone |= set(normalize(n, a.exact))
    with Lock(p["lock"]):
        st = read_state(p)
        if st is None:
            raise Refused("no allowlist is in force; nothing to remove")
        allow = sorted(set(st["allow"]) - gone)
        if allow == st["allow"]:
            digest = render(p, st)
            return report(p, digest, "network: none of those are on the list; nothing changed (" + describe(st) + ")")
        new = dict(st, allow=allow)
        digest = commit(p, new)
        rc = report(p, digest, "network: " + describe(new) if allow else
                    "network: allowlist in force with no names left: nothing outside is reachable until "
                    + until(new["expires"]))
        emit("net_allowlist_changed", f"network allowlist narrowed: {len(allow)} names",
             {"names": len(allow), "removed": len(st["allow"]) - len(allow)}, severity="info")
        return rc


def cmd_clear(a, p):
    with Lock(p["lock"]):
        st = read_state(p)
        if st is None and not os.path.exists(p["state"]):
            digest = render(p, None)
            return report(p, digest, "network: no allowlist is in force")
        if st is not None and not at_terminal():
            raise Refused("lifting the allowlist before it ends widens what the VM can reach, which only you can do, at "
                          "your own terminal: metacomputer net clear. Nothing changed")
        digest = commit(p, None)
        stop_timer(p)
        rc = report(p, digest, "network: allowlist lifted; " + describe(None))
    emit("net_allowlist_lifted", "network allowlist lifted at the terminal", {"why": "cleared"})
    return rc


def cmd_render(a, p):
    """For start: write the rules for the saved list (a list whose time is up is removed first). No confirmation:
    the gateway starts after this, with this file."""
    ended = False
    with Lock(p["lock"]):
        st = read_state(p)
        if st is None and os.path.exists(p["state"]):
            commit(p, None)               # a list whose time ran out while nothing was watching
            ended = True
        else:
            render(p, st)
        if st is not None:
            start_timer(p, st["expires"])
    if ended:
        emit("net_allowlist_lifted", "network allowlist ended", {"why": "ended"}, severity="info")
    return 0


def cmd_expire(a, p):
    ts = int(a.ts)
    while True:
        try:
            with Lock(p["lock"]):
                try:
                    st = read_json(p["state"])
                except (OSError, ValueError):
                    return 0              # not a file this timer understands: the next command reports it
                if not isinstance(st, dict) or st.get("expires") != ts:
                    return 0              # cleared, replaced or extended: another timer (or none) is in charge
                if time.time() >= ts:
                    commit(p, None)
                    done = True
                else:
                    done = False
        except Refused:
            return 0
        if done:
            emit("net_allowlist_lifted", "network allowlist ended", {"why": "ended"}, severity="info")
            return 0
        time.sleep(min(30.0, max(1.0, ts - time.time())))


def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer net", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("allow")
    s.add_argument("names", nargs="+")
    s.add_argument("--for", dest="for_")
    s.add_argument("--exact", action="store_true")
    s = sub.add_parser("remove")
    s.add_argument("names", nargs="+")
    s.add_argument("--exact", action="store_true")
    sub.add_parser("clear")
    sub.add_parser("render")
    s = sub.add_parser("_expire")
    s.add_argument("ts")
    a = ap.parse_args(argv)
    if not a.cmd:
        a = ap.parse_args(["status"] + argv)
    try:
        p = paths()
        return {"status": cmd_status, "allow": cmd_allow, "remove": cmd_remove, "clear": cmd_clear,
                "render": cmd_render, "_expire": cmd_expire}[a.cmd](a, p)
    except Refused as e:
        print(f"metacomputer net: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
