#!/usr/bin/env python3
"""The driver's leftover virtual input devices, removed before a person takes the mouse
(docs/guide.md, "When Claude needs you"; architecture.md, "The hub").

Cua Driver's background input route (path mpx_pointer / mpx_uinput: background pixel clicks, scrolls, typing without
an accessibility route) adds an XInput2 master pointer/keyboard pair named "CUA v1.…" and keeps it between actions:
Cua Driver 0.53.0 holds it for its session until half an hour after the session's last use, then returns it to a pool,
and never removes it. While one exists, Google Chrome ignores the keyboard, and Firefox has been seen ignoring a
person's mouse and keyboard in the live view. The guest's `input-reset` removes them (`xinput remove-master`; their
slaves are left floating, so nothing they send reaches the core pointer or keyboard).

Every reset is made the same way (reset_now): under the dispatch lock held exclusively, so no agent call is sent
while it runs; never while an agent call is still running in the driver, nor when that can't be known (MCP_HUB=off, a
hub that doesn't answer); and recorded before the guest is asked (owners.mark_input_reset), so a lost answer can't
leave a hub sending through removed devices. Who makes one:
  - opening a handoff (lib/handoff.py, at_handoff), after its record is written, once acting calls already in the
    driver have ended (it waits up to INFLIGHT_WAIT_S; otherwise it leaves them and the handoff says it's pending);
  - the command that waits for that handoff (`handoff` itself, or `handoff wait`), when it was left pending: as soon
    as nothing of the agent's runs in the driver any more, while the handoff is still open (at_open_handoff). While
    a pair is left, Chrome ignores the live view's keyboard for the whole handoff;
  - `metacomputer view --open` (bin/metacomputer view_open -> `input_reset.py view`, for_view), and, unless a person
    runs it at their own terminal, only when no other controller is active and no handoff is open;
  - `metacomputer input-reset` (for_cli; during a handoff only from the user's own terminal);
  - the hub's Chrome guard (mcp_hub.Hub.input_reset).
A driver connection that held a pair keeps its id after the pair is gone, and its next pointer action through it ends
the driver (Xlib's XI_BadDevice on XIWarpPointer). So before a call that may use the driver's own devices is sent, the
hub checks, in the same critical section as the send, whether a reset came after the connection began using them, and
reconnects first when nothing would be cut off (mcp_hub.Hub.before_send); nothing else tears it down.

The VM is untrusted: the guest's answer is read with a size limit and a deadline, and only a count is taken from it.

  input_reset.py view --transport N WORD...    view --open's reset (one line on stdout when it says something; exit 0)
  input_reset.py reset [--check] --transport N WORD...   `metacomputer input-reset` (for_cli)
"""
import os
import re
import select
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owners  # noqa: E402
import wirelimits  # noqa: E402

GUEST = "/opt/metacomputer/bin/input-reset"
RESET_TIMEOUT_S = 20.0        # SSH (5 s to connect) and the guest's xinput calls
OUT_MAX = 256                 # the guest prints one count
INFLIGHT_WAIT_S = 10.0        # opening a handoff waits this long for acting calls already in the driver
DISPATCH_TRY_S = 2.0          # a pending reset during a handoff waits this long for the dispatch lock, then tries later
POLL_S = 0.25
COUNT_RE = re.compile(rb"\s*([0-9]{1,4})\s*")
ERR_WAIT_S = 2.0              # how long the end of the command's stderr is waited for, once it has ended
REMOVED = ("Removed {n} leftover virtual input device pair(s) of the agent's driver, so the live view's mouse and "
           "keyboard reach every app.")
TRY_LATER = ("If the live view ignores your mouse or keyboard, run `metacomputer input-reset` in your own terminal.")
# Why opening a handoff left the devices (at_handoff's detail): the waiting command makes the reset later only for the
# first (the second can't be known, so it's never made automatically).
PENDING_RUNNING = "one of its actions was still running in the virtual computer"
PENDING_UNKNOWN = "metacomputer couldn't tell whether one of its actions was still running"


def parse_count(out):
    """The guest's answer (bytes) -> the number of pairs it found, or None when it isn't one number."""
    m = COUNT_RE.fullmatch(out or b"")
    return int(m.group(1)) if m else None


def run_guest(transport, check=False, timeout=RESET_TIMEOUT_S, env=None, capture_err=False):
    """The guest's input-reset over transport (an SSH command line without the remote command). Returns (count, None),
    or (None, why) when it couldn't be run or its answer isn't a count: then whether it removed anything isn't known.
    Records nothing (reset_now does, first). capture_err: why ends with what the command's stderr held, the way the hub
    keeps the driver's (wirelimits.StderrCounter): how many lines of which fixed classes, and metacomputer's own refusals
    (a hub pinned to its VM: the hub's own `metacomputer ssh`) in metacomputer's words from the pinned settings in env;
    never a line of it, since what the command, SSH or the VM wrote there is the VM's text."""
    if not transport:
        return None, "no connection to the virtual computer was given"
    try:
        p = subprocess.Popen(list(transport) + [GUEST] + (["--check"] if check else []), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE if capture_err else subprocess.DEVNULL,
                             start_new_session=True, env=env)
    except OSError as e:
        return None, f"couldn't start ssh ({e.strerror or e})"
    # Its stderr is drained on a thread of its own, keeping counts and classes only (the command never blocks on it).
    errs = wirelimits.StderrCounter(p.stderr) if capture_err else None
    out, too_long, deadline = b"", False, time.monotonic() + timeout
    streams = [p.stdout]
    try:
        while streams:
            left = deadline - time.monotonic()
            if left <= 0:
                _kill(p)
                return None, f"no answer within {int(timeout)} s"
            for r in select.select(streams, [], [], min(left, 0.5))[0]:
                chunk = os.read(r.fileno(), 4096)
                if not chunk:
                    streams.remove(r)
                else:
                    if len(out) + len(chunk) > OUT_MAX:
                        too_long = True
                        streams = []
                        _kill(p)
                        break
                    out += chunk
        try:
            rc = p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(p)
            return None, f"no answer within {int(timeout)} s"
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass
        # Closed only once its reader has seen its end: a descriptor closed under a thread still reading it could be
        # reused by the next file opened, and read from. Until then the reader keeps it (and closes nothing else).
        if errs is None or errs.done.wait(ERR_WAIT_S):
            try:
                if p.stderr:
                    p.stderr.close()
            except OSError:
                pass
    said = ""
    if errs is not None:
        summary = errs.text(wait=ERR_WAIT_S, pin=env if env is not None else os.environ, who="the command")
        said = ": " + summary if summary else ""
    if too_long:
        return None, "the VM's answer was too long"
    n = parse_count(out)
    if rc != 0 or n is None:
        return None, ("couldn't reach the VM over SSH" if rc == 255 else
                      "xinput is missing in the VM (rebuild it)" if rc == 2 else
                      f"the VM's input-reset failed (exit {rc})" if rc != 0 else "the VM's answer wasn't a count") + said
    return n, None


def reset_now(transport, env=None, capture_err=False):
    """The reset itself, for a caller that holds the dispatch lock exclusively and found nothing running in the driver.
    The reset is recorded before the guest is asked (owners.mark_input_reset), so a hub never sends through removed
    devices on the strength of an answer that got lost: only a confirmed "nothing found" puts the earlier record back.
    When the record can't be written, nothing is removed. Returns run_guest's (count, why)."""
    prev = owners.input_reset_at()
    if not owners.mark_input_reset(max(time.time(), prev + 0.001)):
        return None, "couldn't record the reset for the hub, so it wasn't made"
    n, why = run_guest(transport, env=env, capture_err=capture_err)
    if n == 0:
        owners.restore_input_reset(prev)         # confirmed: nothing was removed, nothing to renew
    return n, why


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass
    try:
        p.wait(5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def outside_connections(hub_pid):
    """Whether a driver connection for this VM exists that the shared hub (hub_pid, None when none runs) doesn't own:
    a bridge with MCP_HUB=off or a script that chose a hub of its own (each has its own SSH connection with the MCP
    key, to this VM's port), whose calls nobody can ask about. True, False, or None when the process list can't be read."""
    try:
        table = owners.ps_table(strict=True)
        for pid, (_ppid, comm) in table.items():
            if os.path.basename(comm) != "ssh":
                continue
            cmd = owners.full_command(pid)
            if not owners.MCP_KEY_RE.search(cmd) or "@127.0.0.1" not in cmd or not owners.for_this_vm(cmd):
                continue
            if hub_pid and hub_pid in owners.ancestors(pid, table):
                continue
            return True
        return False
    except Exception:
        return None


def in_flight():
    """Agent calls that may still be acting in the driver (acting calls and the calls a disconnected client left
    running): 0 only when that's known; None when it can't be: MCP_HUB=off here, a driver connection the shared hub
    doesn't own (a client running a hub of its own, whatever this command's setting), a hub that can't say."""
    try:
        import mcp_hub
        if mcp_hub.mode() == "off":
            return None
        hub = mcp_hub.hub_pid()
        if outside_connections(hub) is not False:
            return None
        if not hub:
            return 0
        st = mcp_hub.query_status()
    except Exception:
        return None
    drv = st.get("driver") if isinstance(st, dict) and isinstance(st.get("driver"), dict) else None
    if drv is None:
        return None
    # A hub from before the "acting" count says only how many calls are pending: all of them count then.
    acting = drv.get("acting", drv.get("pending"))
    left = drv.get("left_running", 0)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (acting, left)):
        return None
    return acting + left


def wait_in_flight(budget):
    """in_flight() after waiting up to budget seconds for it to reach 0 (unknown isn't waited out: it stays unknown)."""
    end = time.monotonic() + budget
    while True:
        n = in_flight()
        if n == 0 or n is None or time.monotonic() >= end:
            return n
        time.sleep(POLL_S)


def at_handoff(transport):
    """Opening a handoff: called by lib/handoff.py with the record written and the dispatch lock held exclusively (no
    agent call is sent meanwhile). Acting calls already in the driver get INFLIGHT_WAIT_S to end; when one is still
    running then, or that can't be known (MCP_HUB=off, a hub that doesn't answer), nothing is removed: removing them
    could cut that call off. Returns (status, detail, False): ("removed", n), ("none", 0), ("pending", why) or
    ("failed", why); ("skipped", None) without a transport (handoff.py run directly, as the tests do: bin/metacomputer
    always passes one). Never raises."""
    if not transport:
        return "skipped", None, False
    try:
        busy = wait_in_flight(INFLIGHT_WAIT_S)
        if busy != 0:
            return "pending", (PENDING_RUNNING if busy else PENDING_UNKNOWN), False
        n, why = reset_now(transport)
    except Exception as e:  # the handoff opens whatever happens here
        return "failed", f"unexpected error: {type(e).__name__}", False
    if n is None:
        return "failed", why, False
    return ("removed" if n else "none"), n, False


def at_open_handoff(transport, hid):
    """The reset an open handoff left pending (an agent call was still running in the driver when it opened), made by
    the command that waits for that handoff once nothing is running there any more. Made the way every reset is:
    under the dispatch lock held exclusively (DISPATCH_TRY_S, then later), only while handoff hid is still open, never
    while an agent call is in flight or when that can't be known, recorded first (reset_now). Returns (status,
    detail): ("removed", n), ("none", 0), ("later", None) (a call still running, or the lock busy: try again),
    ("unknown", why) (it can't be known whether one runs: never made), ("failed", why) or ("ended", None) (the
    handoff isn't open any more). Never raises."""
    if not transport:
        return "failed", "no connection to the virtual computer was given"
    try:
        with owners.dispatch(shared=False, timeout=DISPATCH_TRY_S):
            h = owners.open_handoff()
            if not h or h.get("id") != hid:
                return "ended", None
            busy = in_flight()
            if busy is None:
                return "unknown", PENDING_UNKNOWN
            if busy > 0:
                return "later", None
            n, why = reset_now(transport)
    except (TimeoutError, OSError):
        return "later", None
    except Exception as e:  # the handoff goes on whatever happens here
        return "failed", f"unexpected error: {type(e).__name__}"
    if n is None:
        return "failed", why
    return ("removed" if n else "none"), n


def for_view(transport, out=print):
    """`metacomputer view --open`: remove the pairs before Screen Sharing opens, when that can't disturb an agent.
    Under the dispatch lock held exclusively (no acting call is sent while it looks and resets):
      - a handoff open: only at the user's own terminal (its owner's own reset; opening it reset already);
      - otherwise, unless a person runs it at their own terminal (a person at a terminal takes the computer whatever
        runs, as `metacomputer handoff` does), not while another controller is active (owners.others);
      - never while an agent call is in flight in the hub.
    Says what it did in one line (nothing when there was nothing to remove). Always returns 0: view --open goes on."""
    user = owners.at_user_terminal((0,))
    try:
        with owners.dispatch(shared=False, timeout=owners.DISPATCH_WAIT_S):
            h = owners.open_handoff()
            if h and not user:
                return 0                          # the handoff removed them when it opened; it's the user's now
            if not h and not user:
                busy = [o for o in owners.others() if o["active"]]
                if busy:
                    out("Left the agent's virtual input devices as they are: another session is using the virtual "
                        "computer (`metacomputer who`). " + TRY_LATER)
                    return 0
            n = in_flight()
            if n is None or n > 0:
                out("Left the agent's virtual input devices as they are: " +
                    ("an agent call is still running in the virtual computer. " if n else
                     "metacomputer couldn't tell whether an agent call is running. ") + TRY_LATER)
                return 0
            n, why = reset_now(transport)
    except (TimeoutError, OSError):
        out("Left the agent's virtual input devices as they are: an action was on its way to the virtual computer. "
            + TRY_LATER)
        return 0
    if n is None:
        out(f"Couldn't remove the agent's leftover virtual input devices ({why}). " + TRY_LATER)
    elif n:
        out(REMOVED.format(n=n))
    return 0


def for_cli(transport, check=False):
    """`metacomputer input-reset [--check]`. --check only counts (it reads). Otherwise the reset is made the way every
    reset is: under the dispatch lock held exclusively, never while an agent call is in flight (or when that can't be
    known), recorded first (reset_now). While a handoff is open only the user's own terminal goes ahead (their own mouse
    and keyboard); anyone else stops with exit 4. Prints the count; exit 0, 1 failed, 3 not now, 4 the user has it."""
    def err(t):
        print("metacomputer: " + t, file=sys.stderr, flush=True)
    if check:
        n, why = run_guest(transport, check=True)
        if n is None:
            err(f"couldn't count the leftover virtual input devices ({why})")
            return 1
        print(n, flush=True)
        return 0
    user = owners.at_user_terminal((0,))
    try:
        with owners.dispatch(shared=False, timeout=owners.DISPATCH_WAIT_S):
            h = owners.open_handoff()
            if h and not user:
                err(owners.handoff_refusal(h))
                return 4
            if h:
                err("your handoff is open, and this runs at your own terminal: going ahead.")
            busy = in_flight()
            if busy is None or busy > 0:
                err("not now: " + ("an agent call is still running in the virtual computer" if busy else
                                   "metacomputer couldn't tell whether an agent call is running (MCP_HUB=off, a client "
                                   "with a hub of its own, or a hub that didn't answer)") + ", and removing the devices could cut it off. Try again "
                    "once it has ended.")
                return 3
            n, why = reset_now(transport)
    except (TimeoutError, OSError):
        err("not now: an action is on its way to the virtual computer, or a handoff is being opened. Try again.")
        return 3
    if n is None:
        err(f"input reset failed ({why}); the driver will start again before its next background input anyway")
        return 1
    print(n, flush=True)
    return 0


def main(argv):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    strictargs.expect(argv, ["view --transport N ~WORD...", "reset [--check] --transport N ~WORD..."], prog="input_reset.py")
    check = False
    if argv[:1] == ["reset"]:
        check = argv[1:2] == ["--check"]
        argv = ["reset"] + argv[2 if check else 1:]
    transport = []
    if len(argv) >= 3 and argv[1] == "--transport":
        try:
            k = int(argv[2])
        except ValueError:
            k = -1
        if k < 1 or len(argv) < 3 + k:
            print("usage: input_reset.py view --transport N WORD...", file=sys.stderr)
            return 2
        transport, rest = argv[3:3 + k], argv[3 + k:]
    else:
        rest = argv[1:]
    if argv[:1] not in (["view"], ["reset"]) or rest:
        print("usage: input_reset.py view|reset [--check] --transport N WORD...", file=sys.stderr)
        return 2
    return for_view(transport) if argv[0] == "view" else for_cli(transport, check)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
