#!/usr/bin/env python3
"""Handing the virtual computer to the user: `metacomputer handoff` (docs/guide.md, "When Claude needs you").

Some steps are the user's to do: a CAPTCHA or "verify you are human" check, a sign-in they'd rather do
themselves, a 2FA prompt the vault can't fill. Claude never tries to pass them. It hands the computer over
and waits:

  handoff.py --why captcha|login|2fa|payment|other --say "what to do" [--timeout 15m] [--json] [--override]
        Tells the user (a needs_you event, through the event stream's cleaning and limits), prints the same
        instructions, and waits until they hand the computer back. While it's open, every acting call waits
        (owners.open_handoff): the hub's clients, `metacomputer sh`, the vault's fills, and the lifecycle
        commands that check for other controllers. Read-only calls still work.
  handoff.py done [--note TEXT]     the user: finished; Claude carries on (from their own terminal)
  handoff.py cancel [--note TEXT]   the user, or the session that asked: called off; Claude stops and asks
  handoff.py wait [--json]          wait again for the open handoff, or report how the last one ended
  handoff.py status [--json]        the open handoff, if any
  handoff.py guard [--hold-fd N]    exit 4 (and say why) while a handoff is open; with --hold-fd, the dispatch
                                    lock is taken shared on the caller's descriptor N and kept for the caller
                                    (bin/metacomputer's fills and session loads)

Opening one also removes the driver's leftover virtual input devices (lib/input_reset.py), under the dispatch lock it
holds exclusively, so the live view's mouse and keyboard work. When an agent action is still running in the VM (or
that can't be known) it leaves them, since removing them could cut that action off, and the message says the reset is
pending and how to make it; when it fails, the handoff opens anyway and its message says how to do it by hand.
bin/metacomputer passes the SSH command for it first: --transport N WORD...

With INTERACTIVE_PROMPTS=on (off by default: dialogs that wait for the user), opening one for Claude also shows a
dialog on the Mac's screen, "Claude needs you: <--say>", with Open live view (runs `metacomputer view --open`) and
Later. A detached helper (handoff.py _dialog ID) shows it and takes it down when the handoff ends; the text reaches
osascript as an argument of a fixed script, never as part of one. Not over SSH, not without the user's screen
session, and not when the person at this terminal opened the handoff.

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
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flockwait  # noqa: E402
import input_reset  # noqa: E402
import owners  # noqa: E402
import termsafe  # noqa: E402

DEFAULT_TIMEOUT_S = 900
MIN_TIMEOUT_S = 30
OPEN_WAIT_S = 40          # opening waits this long for actions on their way (the hub's input reset takes up to 30 s)
POLL_S = 0.25
TEXT_MAX = 500
EXIT_DONE, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED, EXIT_CANCELLED, EXIT_STOPPED, EXIT_TIME_LIMIT = 0, 1, 2, 3, 4, 5, 124
CODES = {"done": EXIT_DONE, "cancelled": EXIT_CANCELLED, "timed_out": EXIT_TIME_LIMIT, "gone": EXIT_CANCELLED}
EVENT_TYPES = {"done": "handoff_done", "cancelled": "handoff_cancelled", "timed_out": "handoff_timed_out"}
SUBCOMMANDS = ("done", "cancel", "wait", "status", "guard")
TRANSPORT = []   # opening one: the SSH command for the VM's input reset (bin/metacomputer passes --transport N WORD...)
TRANSPORT_PROBLEM = []   # --transport 0: bin/metacomputer couldn't set SSH up (the reset is reported as failed)
# The live view, the way every "needs you" message says it (skills/metacomputer/EVENTS.md uses the same words).
LIVE_VIEW = ("Open the live view: run `metacomputer view --open` on your Mac. Screen Sharing connects with the "
             "live-view password it remembers, or the password is on your clipboard (paste it with Cmd+V when it "
             "asks; it isn't your Mac's password).")
# The needs_you event's action (at most 300 characters after cleaning).
ACTION = ("Run `metacomputer view --open` (Screen Sharing; paste the live-view password with Cmd+V if it asks), "
          "do it in the live view, then run `metacomputer handoff done` (or `metacomputer handoff cancel`)")
SPACES = re.compile(r"\s+")
AGENT_NAMES = {"Claude Code": "Claude", "Codex": "Codex", "OpenClaw": "OpenClaw", "Hermes Agent": "Hermes",
               "Gemini CLI": "Gemini", "Antigravity CLI": "Antigravity",
               "Antigravity app": "Antigravity", owners.UNKNOWN_AGENT: "Your agent"}


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


def agent_word(rec):
    """Who asked, for the user's messages: the short name of the agent whose session opened the handoff ("Codex",
    "OpenClaw", "Hermes", "Gemini"), else "Claude"."""
    return AGENT_NAMES.get(rec.get("agent"), "Claude") if isinstance(rec, dict) else "Claude"


def at_terminal():
    """The user's own terminal: standard input is a terminal and no agent session (Claude Code, Codex, OpenClaw, Hermes
    Agent, Gemini CLI or Antigravity CLI) is above this
    process (owners.at_user_terminal). Codex can give its commands a pseudo-terminal, so a terminal alone isn't the
    user: there, pressing Enter or `handoff done` would be the agent saying the user is done."""
    return owners.at_user_terminal((0,))
# The dialog (INTERACTIVE_PROMPTS=on). argv: mode ("show"; "compose" returns the text instead, for the tests), the
# --say text, seconds until it gives up, and who asks ("Claude", "Codex", "OpenClaw", "Hermes" or "Gemini"). The text is data: AppleScript never
# parses it.
DIALOG_SCRIPT = '''on run argv
	set {mode, msg, secs, who} to {item 1 of argv, item 2 of argv, (item 3 of argv) as integer, item 4 of argv}
	set txt to who & " needs you: " & msg
	if mode is "compose" then return txt
	set r to display dialog txt with title "metacomputer" buttons {"Later", "Open live view"} \u00ac
		default button "Open live view" giving up after secs with icon caution
	if gave up of r then return "gave up"
	return button returned of r
end run'''
DIALOG_TEXT_MAX = 240
DIALOG_OPEN = "Open live view"


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
        w = agent_word(rec)
        msg = {"done": f"The user handed the virtual computer back to {w} after {took} (handoff: {rec['why']})",
               "cancelled": f"The handoff ({rec['why']}) was cancelled after {took}; {w} stops and asks how to go on",
               "timed_out": f"Nobody handed the virtual computer back within {took} (handoff: {rec['why']}); "
                            f"the handoff ended and {w}'s actions are no longer paused"}[s]
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
        out(f"handoff ended at its time limit ({took}) with no answer: {agent_word(rec)}'s actions are no longer paused. "
            "Don't try the check yourself: tell the user it is still waiting for them, and ask how to go on "
            "(a new handoff when they're ready).")
    else:
        out("handoff gone: its record was removed or replaced, so the computer isn't paused for it any more. Ask "
            "the user how to go on.")
    if note:
        out("Their note: " + termsafe.escape_for_terminal(note))
    return CODES[s]


def reset_line(reset, who):
    """What opening the handoff did about the driver's leftover virtual input devices (lib/input_reset.py), in one
    line for the user, or None when there's nothing to say."""
    status, detail, still = reset
    if status == "pending":
        return (f"{who}'s leftover virtual input devices weren't removed: {detail}, and removing them could cut it off. "
                f"{who}'s actions are paused now; once that one has ended, if the live view ignores your mouse or "
                "keyboard, run `metacomputer input-reset` in your own terminal (it runs while the handoff is open).")
    if status == "failed":
        return (f"Note: metacomputer couldn't remove {who}'s leftover virtual input devices ({detail}). If the live "
                "view ignores your mouse or keyboard, end the handoff (`metacomputer handoff cancel`) and run "
                "`metacomputer input-reset` (in your own terminal it also runs while the handoff is open).")
    if status == "skipped":
        return None
    said = (f"Removed {who}'s leftover virtual input devices ({detail} pair(s)), so the live view's mouse and keyboard "
            "reach every app.") if status == "removed" else None
    if still:
        return ((said + " ") if said else "") + (
            f"One of {who}'s actions was still running then: if the live view ignores your mouse or keyboard, run "
            "`metacomputer input-reset` in your own terminal.")
    return said


def instructions(rec, interactive, reset=("skipped", None, False)):
    out(f"metacomputer: handed the virtual computer to you ({rec['why']}). {agent_word(rec)}'s desktop actions are "
        "paused until you hand it back.")
    out("  What to do: " + termsafe.escape_for_terminal(rec["say"]))
    line = reset_line(reset, agent_word(rec))
    if line:
        out("  " + line)
    out("  1. " + LIVE_VIEW)
    out("     From another device: `metacomputer view` shows the SSH tunnel (`metacomputer view --tailnet`: your "
        "own tailnet).")
    out(f"  2. When you're done: metacomputer handoff done   (add --note \"...\" for anything {agent_word(rec)} should know)")
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
                    f"{agent_word(rec)} handed the virtual computer to you ({rec['why']}); its desktop actions are paused until "
                    f"you run `metacomputer handoff done` (or `cancel`), {owners.fmt_s(rec['timeout_s'])} at most",
                    data={"handoff_id": rec["id"], "why": rec["why"], "timeout_s": rec["timeout_s"]},
                    project=rec.get("cwd"), origin="cli")
        return line
    except Exception:
        return None


# --- the dialog (INTERACTIVE_PROMPTS=on) ------------------------------------------------------------
def dialog_text(say):
    """The --say text as the dialog shows it after "Claude needs you: " (or "Codex needs you: ", and so on): cleaned (one line, no control characters)
    and at most DIALOG_TEXT_MAX characters."""
    t = clean_text(say)
    return t if len(t) <= DIALOG_TEXT_MAX else t[:DIALOG_TEXT_MAX - 1] + "\N{HORIZONTAL ELLIPSIS}"


def dialog_off_reason(interactive):
    """None when the dialog may be shown; else why not."""
    if os.environ.get("MCOMP_INTERACTIVE_PROMPTS", "off") != "on":
        return "off"
    if interactive:
        return "you're at this terminal"
    try:
        import liveview
        return liveview.gui_reason()
    except Exception:
        return "couldn't check for the Mac's screen session"


def start_dialog(hid):
    """handoff.py _dialog ID, detached (its own session, no inherited descriptors), so this command goes on at once."""
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid:
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return True
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, 65536)
        os.execv(sys.executable, [sys.executable, "-I", os.path.abspath(__file__), "_dialog", hid])
    except BaseException:
        pass
    os._exit(0)


def run_dialog(hid):
    """The detached helper: the dialog while handoff hid is open (taken down when it ends); Open live view runs
    `metacomputer view --open` (no terminal: Screen Sharing's remembered password, else the clipboard). It applies the
    rules itself, whoever started it (`metacomputer handoff _dialog ID` included): INTERACTIVE_PROMPTS=on, not over
    SSH, the user's screen session, and a handoff that wasn't opened at a terminal (the record's own `interactive`;
    a record without it counts as opened at one). Exit 0 whether or not it showed one."""
    if not re.fullmatch(r"h-[0-9a-f]{12}", hid or ""):
        return 0
    rec = owners.open_handoff()
    if not rec or rec.get("id") != hid:
        return 0
    if dialog_off_reason(rec.get("interactive") is not False) is not None:
        return 0
    secs = str(max(1, min(int(rec["deadline"] - time.time()), owners.HANDOFF_MAX_S)))
    try:
        p = subprocess.Popen(["osascript", "-e", DIALOG_SCRIPT, "show", dialog_text(rec.get("say")), secs, agent_word(rec)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return 1
    while True:
        try:
            out, _ = p.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            h = owners.open_handoff()
            if not h or h.get("id") != hid:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                return 0
    if out.decode("utf-8", "replace").strip() != DIALOG_OPEN:
        return 0
    try:
        subprocess.run([os.path.join(owners.LAB, "bin", "metacomputer"), "view", "--open"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0


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
    interactive = at_terminal()
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


VAULT_MARK = "vault-inflight."     # bin/metacomputer's marker: a vault request not yet known to have ended
VAULT_MARK_NAME = re.compile(r"vault-inflight\.[1-9][0-9]*")   # its whole name: the marker and the shell's pid


def vault_unsettled():
    """The markers of vault fills and session loads whose end the unlock process didn't confirm. Found under the
    exclusive dispatch lock, so none belongs to a command that's still running (those hold it shared)."""
    try:
        return sorted(f for f in os.listdir(owners.OWNERS_DIR) if VAULT_MARK_NAME.fullmatch(f))
    except OSError:
        return []


def drop_marks(marks):
    """The settled vault markers go, through lib/safedelete.py (only marker names, in an owners folder that isn't a
    link and neither is, holds nor reaches a folder some VM uses). Best effort: one kept is settled again next time."""
    try:
        import safedelete
        safedelete.remove_records(owners.OWNERS_DIR, marks, VAULT_MARK_NAME)
    except Exception:
        pass


def settle_vault(budget):
    """`metacomputer secrets settle`: True once the unlock process has finished everything it was sent (or none
    runs), within budget seconds (at least 5)."""
    budget = max(5.0, budget)
    try:
        r = subprocess.run([os.path.join(owners.LAB, "bin", "metacomputer"), "secrets", "settle", "--timeout",
                            str(int(budget))], stdin=subprocess.DEVNULL, capture_output=True, timeout=budget + 10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def open_and_wait(a, say, timeout, interactive, opened):
    settle()
    table = owners.ps_table()
    claude = owners.claude_of(os.getpid(), table)
    t0 = time.monotonic()
    try:
        # Exclusive: every action still on its way to the VM (a call being sent, a shell run starting, a vault fill
        # or session load until it ends) gets there first, and each one after this looks and finds the record.
        held = owners.dispatch(shared=False, timeout=OPEN_WAIT_S)
        held.__enter__()
    except TimeoutError:
        err("an action is still on its way to the virtual computer (a call being sent, a shell run starting, or a "
            "vault fill or session load, which holds it until it ends); nothing was handed over. Wait for it to "
            "end, then run the handoff again.")
        return EXIT_REFUSED
    except OSError as e:
        err(f"couldn't record the handoff ({e}); nothing was handed over")
        return EXIT_ERROR
    try:
        marks = vault_unsettled()
        if marks and not settle_vault(OPEN_WAIT_S - (time.monotonic() - t0)):
            err("a vault fill or session load ended without the unlock process confirming that it stopped, and it "
                "hasn't confirmed since (`metacomputer secrets settle` waits for it); nothing was handed over. Run "
                f"`metacomputer secrets settle`, then the handoff again. (Marked by {', '.join(marks)} in "
                f"{owners.short(owners.OWNERS_DIR)}.)")
            return EXIT_REFUSED
        drop_marks(marks)
        reset = ("skipped", None, False)
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
                   "claude_pid": claude, "agent": owners.agent_of(claude, table) if claude else None,
                   "cwd": os.getcwd(), "sid": owners.my_sid(), "interactive": interactive,
                   "ended": None, "ended_from": None, "note": None}
            opened["id"] = rec["id"]             # before the write: a signal during it still finds the record
            write_record(rec)
        # The driver's leftover virtual input devices go before the user takes the mouse (lib/input_reset.py): still
        # under the dispatch lock held exclusively, so no agent call is sent meanwhile, and after the record, so every
        # one after it finds the handoff. The handoff is open whatever this does.
        reset = (("failed", TRANSPORT_PROBLEM[0], False) if TRANSPORT_PROBLEM else input_reset.at_handoff(TRANSPORT))
    except (OSError, TimeoutError) as e:
        err(f"couldn't record the handoff ({e}); nothing was handed over")
        return EXIT_ERROR
    finally:
        held.__exit__(None, None, None)
    told = announce(rec, interactive)
    off = dialog_off_reason(interactive)
    shown = off is None and start_dialog(rec["id"])
    instructions(rec, interactive, reset)
    if shown:
        out("  A dialog on the Mac's screen says the same, with an Open live view button (INTERACTIVE_PROMPTS=on).")
    if told:
        out("  " + told)
    if not interactive:
        out("  For you, the agent: tell the user the same in your reply, then wait for this command to end. Exit 0: they're "
            "done, read the screen again and carry on; 4: cancelled, stop and ask; 124: time limit, ask (don't "
            "try the check yourself).")
    return wait_for(rec["id"], interactive, a.json)


def cmd_end(a, to):
    note = clean_text(a.note) or None
    interactive = at_terminal()
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
    try:
        table = owners.ps_table(strict=True)
        known = owners.ancestry_complete(os.getpid(), table)
    except OSError:
        table, known = {}, False
    claude = owners.claude_of(os.getpid(), table) if table else None
    own_session = bool(claude) and claude == rec.get("claude_pid")
    if to == "done" and not interactive and not known:
        err("couldn't tell whether this runs inside an agent session (the process list couldn't be read all the way "
            "up), so it can't say the user is done. The user runs `metacomputer handoff done` in their own terminal.")
        return EXIT_REFUSED
    if to == "done" and claude and not interactive:
        # Any agent session, not only the one that asked: another agent's session (or one started from the session
        # that asked) saying "done" would be an agent saying the user is done.
        who = ("this " + (rec.get("agent") if rec.get("agent") in owners.AGENTS else "Claude Code")
               + " session asked for the handoff, so it" if own_session
               else "an agent session (Claude Code, Codex, OpenClaw, Hermes Agent, Gemini CLI or Antigravity CLI)")
        err(f"{who} can't say the user is done. The user runs "
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
        out(f"That handoff had already reached its time limit, so {agent_word(new)}'s actions weren't paused any more.")
    elif new["status"] == "done":
        out(f"Handed the virtual computer back to {agent_word(new)}" + (" with your note." if note else "."))
    else:
        out(f"Cancelled the handoff: {agent_word(new)} stops this step and asks you how to go on.")
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
    interactive = at_terminal()
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


def cmd_guard(argv):
    """guard [--hold-fd N]: exit 4, and say why, while a handoff is open. With --hold-fd, N is a descriptor the
    caller (bin/metacomputer) opened on this VM's dispatch lock file: the lock is taken shared on it first, and
    stays with the caller's descriptor after this exits (a flock belongs to the open file, which the caller
    shares), so no handoff opens until the caller and what it runs are done."""
    if argv:
        if len(argv) != 2 or argv[0] != "--hold-fd" or not argv[1].isdigit() or not 3 <= int(argv[1]) <= 255:
            err("usage: guard [--hold-fd N]")
            return EXIT_USAGE
    # (MCOMP_PARSE_ONLY, tests/test_help.py: taken; nothing checked or held)
    strictargs.parsed("handoff.py guard", {"flags": [], "values": ["--hold-fd"], "words": 1, "max_words": 1})
    if argv:
        fd = int(argv[1])
        try:
            st, want = os.fstat(fd), os.lstat(owners.dispatch_path())
        except OSError as e:
            err(f"couldn't check this VM's dispatch lock ({e}); not going ahead")
            return EXIT_CANCELLED
        if not stat.S_ISREG(want.st_mode) or (st.st_dev, st.st_ino) != (want.st_dev, want.st_ino):
            err(f"descriptor {fd} isn't this VM's dispatch lock ({owners.dispatch_path()}); not going ahead")
            return EXIT_CANCELLED
        try:
            flockwait.take(fd, owners.DISPATCH_WAIT_S, 0.02, shared=True)
        except BlockingIOError:
            err("a handoff is being opened; not going ahead (`metacomputer handoff status`)")
            return EXIT_CANCELLED
    h = owners.open_handoff()
    if h:
        print("metacomputer: " + owners.handoff_refusal(h), file=sys.stderr, flush=True)
        return EXIT_CANCELLED
    return 0


def sub_parser(sub):
    ap = strictargs.Parser(prog=f"metacomputer handoff {sub}")
    if sub in ("done", "cancel"):
        ap.add_argument("--note", action="text", help="what Claude should know (at most 500 characters)")
    if sub in ("wait", "status"):
        ap.add_argument("--json", action="store_true")
    return ap


def open_parser():
    ap = strictargs.Parser(
        prog="metacomputer handoff",
        description="Hand the virtual computer to the user and wait until they hand it back "
                    "(docs/guide.md, \"When Claude needs you\"). Also: done [--note], cancel [--note], wait, status.")
    ap.add_argument("--why", default="other", choices=owners.HANDOFF_WHY)
    ap.add_argument("--say", action="text", required=True, help="what the user should do, in one line (no secrets or full links)")
    ap.add_argument("--timeout", default=f"{DEFAULT_TIMEOUT_S // 60}m", help="time limit (default 15m, at most 2h)")
    ap.add_argument("--json", action="store_true", help="the outcome as one JSON line at the end")
    ap.add_argument("--override", action="store_true", help="open it while another session is using the computer "
                                                            "(only after the user agreed)")
    return ap


VALUE_OPTIONS = ("--why", "--say", "--timeout", "--note")     # what open_parser and sub_parser take a value for


def asks_help(words):
    """Whether -h or --help is a flag among words, by the parsers' own rules: an option's value stays a value
    (`--say --help` is the parser's missing-value error), and `--` ends the options."""
    skip = False
    for a in words:
        if skip:
            skip = False
        elif a == "--":
            return False
        elif a in ("-h", "--help"):
            return True
        elif a in VALUE_OPTIONS:
            skip = True
    return False


def main(argv):
    if argv[:1] == ["--transport"]:
        # bin/metacomputer's SSH command for the VM: a request for usage among its words is a broken command line,
        # refused (never taken for usage, and never an option's value followed by an action: `--transport 1 --help
        # cancel` once cancelled the open handoff).
        try:
            k = int(argv[1]) if len(argv) > 1 else -1
        except ValueError:
            k = -1
        if k >= 0 and any(a in ("-h", "--help") for a in argv[2:2 + k]):
            err("--transport N WORD... (bin/metacomputer's): a request for usage isn't a word of it")
            return EXIT_USAGE
    rest = argv[2 + int(argv[1]):] if argv[:1] == ["--transport"] and len(argv) > 1 and argv[1].isdigit() else argv
    if asks_help(rest):
        # Usage, and nothing else, before anything is done.
        words = [a for a in rest if a in ("done", "cancel", "wait", "status")]
        (sub_parser(words[0]) if rest[:1] == words[:1] and words else open_parser()).print_help()
        return 0
    if argv[:1] == ["--transport"]:
        # bin/metacomputer's SSH command for the VM (lib/input_reset.py), ahead of the user's arguments
        try:
            k = int(argv[1]) if len(argv) > 1 else -1
        except ValueError:
            k = -1
        if k < 0 or len(argv) < 2 + k:
            err("--transport N WORD... (bin/metacomputer's)")
            return EXIT_USAGE
        TRANSPORT[:] = argv[2:2 + k]
        if k == 0:
            TRANSPORT_PROBLEM[:] = ["couldn't set up SSH to the VM"]
        argv = argv[2 + k:]
    if len(argv) == 2 and argv[0] == "_dialog":
        strictargs.parsed("handoff.py _dialog", {"flags": [], "values": [], "words": 2, "max_words": 2})
        return run_dialog(argv[1])
    if argv and argv[0] in SUBCOMMANDS:
        sub, rest = argv[0], argv[1:]
        if sub == "guard":
            return cmd_guard(rest)
        a = sub_parser(sub).parse_args(rest)
        try:
            if sub in ("done", "cancel"):
                return cmd_end(a, sub)
            return cmd_wait(a) if sub == "wait" else cmd_status(a)
        except (OSError, TimeoutError) as e:
            err(str(e))
            return EXIT_ERROR
    return cmd_open(open_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
