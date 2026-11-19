#!/usr/bin/env python3
"""Handing the virtual computer to the user: `metacomputer handoff` (docs/guide.md, "When Claude needs you").

Some steps are the user's to do: a CAPTCHA or "verify you are human" check, a sign-in they'd rather do
themselves, a 2FA prompt the vault can't fill. Claude never tries to pass them. It hands the computer over
and waits:

  handoff.py --why captcha|login|2fa|other --say "what to do" [--timeout 15m] [--json] [--override]
        Tells the user (a needs_you event, through the event stream's cleaning and limits), prints the same
        instructions, and waits until they hand the computer back. While it's open, every acting call waits
        (owners.open_handoff): the hub's clients, `metacomputer sh`, the vault's fills, and the lifecycle
        commands that check for other controllers. Read-only calls still work.
  handoff.py done [--note TEXT]     the user: finished; Claude carries on (from their own terminal)
  handoff.py cancel [--note TEXT]   the user, or the session that asked: called off; Claude stops and asks
  handoff.py wait [--json]          wait again for the open handoff, or report how the last one ended
  handoff.py status [--json]        the open handoff, if any
  handoff.py guard                  exit 4 (and say why) while a handoff is open (bin/metacomputer's fills)

At a terminal, Enter where `handoff` waits is "done", and c then Enter (or Ctrl-C) is "cancel".

Exit status of the commands that wait: 0 done, 4 cancelled, 124 time limit (the computer is Claude's again),
5 stopped waiting while it stays open (a signal), 3 refused (one is open already, or another controller is
active), 2 usage, 1 couldn't record it. The record (.run/owners/handoff.state, owners.py) is written only here,
under the owners lock; each change of state is made once, by whoever makes it, and that process sends its event.
"""
import argparse
import contextlib
import json
import os
import re
import secrets
import select
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owners  # noqa: E402
import termsafe  # noqa: E402

DEFAULT_TIMEOUT_S = 900
MIN_TIMEOUT_S = 30
POLL_S = 0.25
TEXT_MAX = 500
EXIT_DONE, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED, EXIT_CANCELLED, EXIT_STOPPED, EXIT_TIME_LIMIT = 0, 1, 2, 3, 4, 5, 124
CODES = {"done": EXIT_DONE, "cancelled": EXIT_CANCELLED, "timed_out": EXIT_TIME_LIMIT, "gone": EXIT_CANCELLED}
EVENT_TYPES = {"done": "handoff_done", "cancelled": "handoff_cancelled", "timed_out": "handoff_timed_out"}
SUBCOMMANDS = ("done", "cancel", "wait", "status", "guard")
# The live view, the way every "needs you" message says it (skills/metacomputer/EVENTS.md uses the same words).
LIVE_VIEW = ("Run `metacomputer view --open` on your Mac: Screen Sharing opens the live view, and the live-view "
             "password is on your clipboard (paste it with Cmd+V when it asks; it isn't your Mac's password).")
# The needs_you event's action (at most 300 characters after cleaning).
ACTION = ("Run `metacomputer view --open` (it puts the live-view password on your clipboard: paste with Cmd+V), "
          "do it in the live view, then run `metacomputer handoff done` (or `metacomputer handoff cancel`)")
SPACES = re.compile(r"\s+")


class Stop(BaseException):
    """A signal stopped this command (an agent's tool ended it). Not an Exception, so best-effort code (events)
    can't swallow it."""


@contextlib.contextmanager
def stoppable(interactive):
    """SIGTERM and SIGHUP (and SIGINT without a terminal: Ctrl-C at one is "cancel") raise Stop, for as long as
    the handoff is being opened or waited for, so the command always says where it stopped."""
    def stop(signum, frame):
        raise Stop(signum)
    sigs = [signal.SIGTERM, signal.SIGHUP] + ([] if interactive else [signal.SIGINT])
    old = {s: signal.signal(s, stop) for s in sigs}
    try:
        yield
    finally:
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (OSError, ValueError):
                pass


def stopped(hid, as_json):
    rec = owners.open_handoff()
    still = bool(rec and rec.get("id") == hid)
    left = f", or its time limit at {until(rec['deadline'])}" if still else ""
    if as_json:
        out(json.dumps({"status": "waiting_stopped", "id": hid, "open": still}))
    else:
        out("handoff stopped waiting; the handoff stays open (Claude's actions stay paused) until the user hands "
            f"the computer back or cancels it{left}. `metacomputer handoff wait` waits again.")
    return EXIT_STOPPED


def out(text=""):
    print(text, flush=True)


def err(text):
    print(f"metacomputer handoff: {text}", file=sys.stderr, flush=True)


def clean_text(s):
    """One line of at most TEXT_MAX characters, without control characters or bidi overrides."""
    s = termsafe.UNSAFE.sub(" ", str(s or ""))
    s = SPACES.sub(" ", s).strip()
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX - 1] + "\N{HORIZONTAL ELLIPSIS}"


def duration(v):
    """'900', '900s', '15m', '1h' -> seconds; None if it isn't one."""
    m = re.fullmatch(r"\s*(\d{1,6}(?:\.\d+)?)\s*([smh]?)\s*", str(v or ""))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def until(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# --- the record -----------------------------------------------------------------------------------
def write_record(rec):
    """Atomically, as a new 0600 file renamed over the old one (a link there is replaced, never followed)."""
    os.makedirs(owners.OWNERS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=owners.OWNERS_DIR, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, owners.handoff_path())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def remove_record():
    try:
        os.remove(owners.handoff_path())
    except FileNotFoundError:
        pass


def emit_end(rec):
    """The event for a change this process made (best effort: it never fails the command)."""
    try:
        import events
        s = rec["status"]
        took = owners.fmt_s(rec["ended"] - rec["opened"])
        msg = {"done": f"The user handed the virtual computer back to Claude after {took} (handoff: {rec['why']})",
               "cancelled": f"The handoff ({rec['why']}) was cancelled after {took}; Claude stops and asks how to go on",
               "timed_out": f"Nobody handed the virtual computer back within {took} (handoff: {rec['why']}); "
                            "the handoff ended and Claude's actions are no longer paused"}[s]
        events.emit("lifecycle", EVENT_TYPES[s], msg, "warn" if s == "timed_out" else "info",
                    data={"handoff_id": rec["id"], "why": rec["why"], "open_s": int(rec["ended"] - rec["opened"]),
                          "ended_from": rec.get("ended_from"), "has_note": bool(rec.get("note"))},
                    project=rec.get("cwd"), origin="cli")
    except Exception:
        pass


def end(hid, to, ended_from, note=None):
    """The open handoff hid (None: whichever is open) -> to ("done", "cancelled", "timed_out"), under the owners
    lock. Returns (the record afterwards, whether this call changed it). A "done" or "cancel" that comes after
    the deadline finds a handoff that had already ended: it's recorded as timed out. "cancel" removes a record
    that can't be read."""
    with owners.lock():
        rec = owners.handoff_record()
        if rec is None:
            return None, False
        if rec.get("unreadable"):
            if to == "cancelled" and hid is None:
                remove_record()
                return {**rec, "status": "gone"}, True
            return rec, False
        if (hid is not None and rec["id"] != hid) or rec["status"] != "open":
            return rec, False
        now = time.time()
        if now >= rec["deadline"]:
            to, ended_from, note, now = "timed_out", "time_limit", None, rec["deadline"]
        rec.update(status=to, ended=now, ended_from=ended_from, note=note)
        write_record(rec)
    emit_end(rec)
    return rec, True


def settle():
    """Record the time limit of an open handoff whose deadline passed with nobody watching (its waiter was
    stopped): once, with its event. The guards don't need this (an open record past its deadline doesn't count),
    it's for the record and the event."""
    rec = owners.handoff_record()
    if rec and not rec.get("unreadable") and rec["status"] == "open" and time.time() >= rec["deadline"]:
        try:
            end(rec["id"], "timed_out", "time_limit")
        except (OSError, TimeoutError):
            pass


# --- reporting ------------------------------------------------------------------------------------
def report(rec, as_json, hid=None):
    """How a handoff ended, for Claude (and the person who ran it). Returns the exit status."""
    s = rec.get("status") if rec else "gone"
    if s not in CODES:
        s = "gone"
    waited = int((rec.get("ended") or time.time()) - rec["opened"]) if rec and rec.get("opened") else None
    note = rec.get("note") if rec else None
    if as_json:
        out(json.dumps({"status": s, "id": (rec or {}).get("id") or hid, "why": (rec or {}).get("why"),
                        "waited_s": waited, "ended_from": (rec or {}).get("ended_from"), "note": note}))
        return CODES[s]
    took = owners.fmt_s(waited or 0)
    if s == "done":
        out(f"handoff done: the user handed the virtual computer back after {took}. Read the screen again before "
            "you act: it changed while you waited, and element tokens from before are stale.")
    elif s == "cancelled":
        who = " (by the session that asked for it)" if rec.get("ended_from") == "claude_session" else ""
        out(f"handoff cancelled after {took}{who}: stop this step and ask the user how to go on. Don't try the "
            "check again yourself.")
    elif s == "timed_out":
        out(f"handoff ended at its time limit ({took}) with no answer: Claude's actions are no longer paused. "
            "Don't try the check yourself: tell the user it is still waiting for them, and ask how to go on "
            "(a new handoff when they're ready).")
    else:
        out("handoff gone: its record was removed or replaced, so the computer isn't paused for it any more. Ask "
            "the user how to go on.")
    if note:
        out("Their note: " + termsafe.escape_for_terminal(note))
    return CODES[s]


def instructions(rec, interactive):
    out(f"metacomputer: handed the virtual computer to you ({rec['why']}). Claude's desktop actions are paused "
        "until you hand it back.")
    out("  What to do: " + termsafe.escape_for_terminal(rec["say"]))
    out("  1. " + LIVE_VIEW)
    out("     From another device: `metacomputer view` shows the SSH tunnel (`metacomputer view --tailnet`: your "
        "own tailnet).")
    out("  2. When you're done: metacomputer handoff done   (add --note \"...\" for anything Claude should know)")
    out("     To call it off:   metacomputer handoff cancel")
    if interactive:
        out("     Or here: press Enter when you're done; c and Enter, or Ctrl-C, calls it off.")
    out(f"  Waiting up to {owners.fmt_s(rec['deadline'] - rec['opened'])} (until {until(rec['deadline'])}).")


# --- waiting --------------------------------------------------------------------------------------
def wait_for(hid, interactive, as_json):
    """Until the handoff hid ends: the user's `handoff done|cancel`, Enter at this terminal, or its time limit.
    A signal (the agent's tool stopped this command) stops only the waiting: the handoff stays open. The caller
    holds stoppable()."""
    reading = interactive
    try:
        while True:
            rec = owners.handoff_record()
            if rec is None or (rec.get("id") != hid and not rec.get("unreadable")):
                return report(None, as_json, hid)
            if rec["status"] != "open":
                return report(rec, as_json)
            if time.time() >= rec["deadline"]:
                try:
                    done, _ = end(hid, "timed_out", "time_limit")
                except (OSError, TimeoutError):
                    done = None
                if done is None or done.get("unreadable"):
                    done = {**rec, "status": "timed_out", "ended": rec["deadline"]}
                return report(done, as_json)
            if reading:
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], POLL_S)
                except (OSError, ValueError):
                    reading = False
                    continue
                if ready:
                    line = sys.stdin.readline()
                    if not line:                 # end of input: stop reading, keep waiting
                        reading = False
                        continue
                    try:
                        end(hid, "cancelled" if line.strip().lower() in ("c", "cancel") else "done", "enter")
                    except (OSError, TimeoutError) as e:
                        err(f"couldn't record that ({e}); try again, or run `metacomputer handoff done`")
            else:
                time.sleep(POLL_S)
    except KeyboardInterrupt:                    # Ctrl-C at the terminal that waits: the person there calls it off
        try:
            end(hid, "cancelled", "ctrl_c")
        except (OSError, TimeoutError):
            pass
        rec = owners.handoff_record()
        return report(rec if rec and rec.get("id") == hid else None, as_json, hid)
    except Stop:
        return stopped(hid, as_json)


# --- commands -------------------------------------------------------------------------------------
def announce(rec, interactive):
    """The needs_you notice (as `metacomputer notify` sends it: Claude's words, cleaned, the same limits and
    5-minute repeat rule) and lifecycle.handoff_opened. Best effort. Returns a line on who was told, or None."""
    try:
        import events
        line = None
        if not interactive:                      # someone at this terminal handed it to themselves: no ping
            ev = events.emit("needs_you", rec["why"], rec["say"], "warn", ACTION,
                             {"why": rec["why"], "declared_by": "claude", "handoff_id": rec["id"],
                              "timeout_s": rec["timeout_s"]},
                             project=rec.get("cwd"), debounce=events.content_key(rec["why"], rec["say"], ACTION),
                             debounce_s=events.NOTIFY_DUP_S, origin="claude")
            if ev is None:
                line = (f"Not sent again: the same message went out within {events.NOTIFY_DUP_S // 60} minutes. "
                        "Say it in your reply.")
            else:
                rep = events.delivery_report(events.load_config(), ev)
                line = ("notified: " + "; ".join(f"{n} {s}" for n, s in rep)) if rep else \
                    "No event sink takes needs_you events, so nobody was pinged: say it in your reply."
        events.emit("lifecycle", "handoff_opened",
                    f"Claude handed the virtual computer to you ({rec['why']}); its desktop actions are paused until "
                    f"you run `metacomputer handoff done` (or `cancel`), {owners.fmt_s(rec['timeout_s'])} at most",
                    data={"handoff_id": rec["id"], "why": rec["why"], "timeout_s": rec["timeout_s"]},
                    project=rec.get("cwd"), origin="cli")
        return line
    except Exception:
        return None


def cmd_open(a):
    say = clean_text(a.say)
    if not say:
        err('--say needs what the user should do, e.g. --say "Tick the \'Verify you are human\' box on example.com"')
        return EXIT_USAGE
    timeout = duration(a.timeout)
    if timeout is None or not MIN_TIMEOUT_S <= timeout <= owners.HANDOFF_MAX_S:
        err(f"--timeout takes {MIN_TIMEOUT_S} s to {owners.HANDOFF_MAX_S // 3600} h (15m, 900, 1h)")
        return EXIT_USAGE
    if os.environ.get("MCOMP_VM_STATE", "running") != "running":
        err("the virtual computer is off, so there's nothing to hand over: `metacomputer start` first")
        return EXIT_ERROR
    interactive = sys.stdin.isatty()
    opened = {}
    with stoppable(interactive):
        try:
            return open_and_wait(a, say, timeout, interactive, opened)
        except Stop:
            h = owners.open_handoff()
            if opened.get("id") and h and h.get("id") == opened["id"]:
                return stopped(opened["id"], a.json)
            err("stopped before anything was handed over")
            return EXIT_ERROR
        except KeyboardInterrupt:                # Ctrl-C at this terminal before the wait began: called off
            rec = owners.handoff_record()
            if not opened.get("id") or not rec or rec.get("id") != opened["id"]:
                err("stopped before anything was handed over")
                return EXIT_ERROR
            try:
                rec = end(opened["id"], "cancelled", "ctrl_c")[0] or rec
            except (OSError, TimeoutError):
                pass
            return report(rec, a.json, opened["id"])


def open_and_wait(a, say, timeout, interactive, opened):
    settle()
    table = owners.ps_table()
    claude = owners.claude_of(os.getpid(), table)
    try:
        with owners.lock():
            cur = owners.open_handoff()
            if cur:
                err("a handoff is already open: " + owners.handoff_describe(cur) +
                    ". Wait for it (`metacomputer handoff wait`); don't open another.")
                return EXIT_REFUSED
            # Opening one acts for the one-controller rule: Claude doesn't pause another session's work. A person
            # at a terminal takes the computer whatever runs (that's what a handoff is for), and so does --override,
            # after the user agreed.
            if not (interactive or a.override):
                busy = [o for o in owners.others(table=table) if o["active"]]
                if busy:
                    err("the virtual computer is in use by another controller:\n" +
                        "\n".join("  - " + owners.describe(o) for o in busy) +
                        "\nAsk the user; if they agree, run the handoff again with --override.")
                    return EXIT_REFUSED
            now = time.time()
            rec = {"v": 1, "id": "h-" + secrets.token_hex(6), "status": "open", "why": a.why, "say": say,
                   "opened": now, "deadline": now + timeout, "timeout_s": int(timeout), "opener_pid": os.getpid(),
                   "claude_pid": claude, "cwd": os.getcwd(), "sid": owners.my_sid(), "interactive": interactive,
                   "ended": None, "ended_from": None, "note": None}
            opened["id"] = rec["id"]             # before the write: a signal during it still finds the record
            write_record(rec)
    except (OSError, TimeoutError) as e:
        err(f"couldn't record the handoff ({e}); nothing was handed over")
        return EXIT_ERROR
    told = announce(rec, interactive)
    instructions(rec, interactive)
    if told:
        out("  " + told)
    if not interactive:
        out("  For Claude: tell the user the same in your reply, then wait for this command to end. Exit 0: they're "
            "done, read the screen again and carry on; 4: cancelled, stop and ask; 124: time limit, ask (don't "
            "try the check yourself).")
    return wait_for(rec["id"], interactive, a.json)


def cmd_end(a, to):
    note = clean_text(a.note) or None
    interactive = sys.stdin.isatty()
    settle()
    rec = owners.handoff_record()
    if rec is None or (rec["status"] != "open" and not rec.get("unreadable")) or (
            not rec.get("unreadable") and time.time() >= rec["deadline"]):
        last = f" (the last one ended: {rec['status'].replace('_', ' ')})" if rec and rec.get("status") else ""
        err("no handoff is open" + last)
        return EXIT_ERROR
    if rec.get("unreadable") and to == "done":
        err(owners.handoff_describe(rec) + "; nothing to hand back")
        return EXIT_ERROR
    claude = owners.claude_of(os.getpid(), owners.ps_table())
    own_session = bool(claude) and claude == rec.get("claude_pid")
    if to == "done" and own_session and not interactive:
        err("this Claude Code session asked for the handoff, so it can't say the user is done. The user runs "
            "`metacomputer handoff done` in their own terminal (or presses Enter where `metacomputer handoff` "
            "waits at a terminal). Ask them; if they've changed their mind, `metacomputer handoff cancel` calls it off.")
        return EXIT_REFUSED
    ended_from = "terminal" if interactive else ("claude_session" if own_session else "process")
    try:
        new, changed = end(None if rec.get("unreadable") else rec["id"], "cancelled" if to == "cancel" else "done",
                           ended_from, note)
    except (OSError, TimeoutError) as e:
        err(f"couldn't record it ({e}); try again")
        return EXIT_ERROR
    if not changed or new is None:
        err("no handoff is open (it ended meanwhile" + (f": {new['status'].replace('_', ' ')})" if new else ")"))
        return EXIT_ERROR
    if new["status"] == "gone":
        out("Removed the handoff record that couldn't be read; acting calls aren't paused for it any more.")
    elif new["status"] == "timed_out":
        out("That handoff had already reached its time limit, so Claude's actions weren't paused any more.")
    elif new["status"] == "done":
        out("Handed the virtual computer back to Claude" + (" with your note." if note else "."))
    else:
        out("Cancelled the handoff: Claude stops this step and asks you how to go on.")
    return EXIT_DONE


def public(rec):
    if not rec:
        return None
    keys = ("id", "status", "why", "say", "opened", "deadline", "timeout_s", "cwd", "ended", "ended_from", "note",
            "unreadable")
    return {k: rec.get(k) for k in keys if k in rec}


def cmd_wait(a):
    settle()
    rec = owners.handoff_record()
    if rec is None:
        err("no handoff to wait for")
        return EXIT_ERROR
    if rec["status"] != "open":
        return report(rec, a.json)               # it ended while nobody waited: how
    interactive = sys.stdin.isatty()
    with stoppable(interactive):
        try:
            return wait_for(rec.get("id"), interactive, a.json)
        except Stop:
            return stopped(rec.get("id"), a.json)


def cmd_status(a):
    settle()
    rec = owners.handoff_record()
    h = owners.open_handoff()
    if a.json:
        out(json.dumps({"open": bool(h), "handoff": public(rec)}))
        return 0
    if h:
        out(owners.handoff_describe(h))
        if h.get("say"):
            out("  What to do: " + termsafe.escape_for_terminal(h["say"]))
    else:
        last = ""
        if rec and rec.get("status") != "open":
            last = f" The last one ({rec['why']}) ended {rec['status'].replace('_', ' ')}"
            last += f", {owners.fmt_s(time.time() - rec['ended'])} ago." if rec.get("ended") else "."
        out("No handoff is open." + last)
    return 0


def cmd_guard():
    h = owners.open_handoff()
    if h:
        print("metacomputer: " + owners.handoff_refusal(h), file=sys.stderr, flush=True)
        return EXIT_CANCELLED
    return 0


def main(argv):
    if argv and argv[0] in SUBCOMMANDS:
        sub, rest = argv[0], argv[1:]
        if sub == "guard":
            return cmd_guard()
        ap = argparse.ArgumentParser(prog=f"metacomputer handoff {sub}")
        if sub in ("done", "cancel"):
            ap.add_argument("--note", help="what Claude should know (at most 500 characters)")
        if sub in ("wait", "status"):
            ap.add_argument("--json", action="store_true")
        a = ap.parse_args(rest)
        try:
            if sub in ("done", "cancel"):
                return cmd_end(a, sub)
            return cmd_wait(a) if sub == "wait" else cmd_status(a)
        except (OSError, TimeoutError) as e:
            err(str(e))
            return EXIT_ERROR
    ap = argparse.ArgumentParser(
        prog="metacomputer handoff",
        description="Hand the virtual computer to the user and wait until they hand it back "
                    "(docs/guide.md, \"When Claude needs you\"). Also: done [--note], cancel [--note], wait, status.")
    ap.add_argument("--why", default="other", choices=owners.HANDOFF_WHY)
    ap.add_argument("--say", required=True, help="what the user should do, in one line (no secrets or full links)")
    ap.add_argument("--timeout", default=f"{DEFAULT_TIMEOUT_S // 60}m", help="time limit (default 15m, at most 2h)")
    ap.add_argument("--json", action="store_true", help="the outcome as one JSON line at the end")
    ap.add_argument("--override", action="store_true", help="open it while another session is using the computer "
                                                            "(only after the user agreed)")
    a = ap.parse_args(argv)
    return cmd_open(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
