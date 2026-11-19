"""Human pacing for browser windows (BROWSER_PACING; skills/metacomputer/BROWSER.md, "Human pacing").

The hub (lib/mcp_hub.py) calls this, after every guard and right before an acting call leaves for the driver, for
type_text and run_steps aimed at a Firefox or Chrome window, so it applies to every client. With
BROWSER_PACING=human (the default) it changes only the call's own arguments, with what Cua Driver offers:
- type_text's delay_ms, the driver's pause between characters (one value for a whole call, 0..200 ms), set to a
  random value in KEY_MS. In a run_steps batch, a text of RUN_MIN_CHARS or more is typed in two or three runs,
  split at random places, each with its own pause, so the rhythm changes within a field. The driver can't vary
  the pause key by key, and each extra run costs about RUN_COST_S of driver time, so runs stay few.
- run_steps' delay_ms, its pause between steps, set to a random value in STEP_MS when a step types into, or clicks
  in, a browser window.
- before a pixel click (x,y) in a browser window, a few move_cursor steps at desktop scope carry the real pointer
  along a gentle curve toward the target; the click itself lands exactly where it was aimed. Only pixel clicks:
  the hub doesn't know where a click by element token or by role and name lands (the driver resolves it, and moves
  the pointer straight there as part of the click).
A delay_ms the caller set (0 included) is kept. At most CAP_S is added per call, and never so much that a batch
could pass the driver's own limits (MAX_STEPS steps, BATCH_LIMIT_S); a batch too long for any pacing is left as it
is. BROWSER_PACING=fast changes nothing.

Nothing here logs or returns text: a plan holds counts and milliseconds. The secrets vault types on its own path
(SSH to the VM), never through the hub, so its fills never come here.
"""
import math
import random

MODES = ("human", "fast")
DEFAULT = "human"
KEY_MS = (60, 140)          # type_text delay_ms per character (the driver takes 0..200)
STEP_MS = (120, 320)        # run_steps delay_ms between steps (the driver takes 0..2000)
RUN_MIN_CHARS = 16          # a text this long is typed in 2 runs; from three times this long, in 2 to MAX_RUNS
MAX_RUNS = 3
RUN_MIN = 3                 # characters in a run, at least
RUN_COST_S = 0.8            # driver time of one more foreground type_text step (measured on Cua Driver 0.53.0)
MOVE_COST_S = 0.03          # driver time of one move_cursor step at desktop scope (measured: about 25 ms)
STEP_COST_S = 0.8           # what each of a batch's own steps is counted at, against BATCH_LIMIT_S
CAP_S = 15.0                # added time per call, at most
MAX_STEPS = 64              # the driver's run_steps limit
BATCH_LIMIT_S = 60.0        # the driver's limit for a whole batch
BATCH_MARGIN_S = 20.0       # kept free of pacing for the batch's own waits and checks
PATH_MIN_PX = 40            # a pointer closer than this to the target gets no path
CLICK_TOOLS = ("click", "double_click", "right_click")
FIELD_TOOLS = ("type_text", "set_value", "press_key", "hotkey") + CLICK_TOOLS
STEP_KEYS = ("wait_for", "expect", "timeout_ms")


def mode(env):
    """BROWSER_PACING from the environment (MCOMP_BROWSER_PACING): human (the default, also for an unknown value) or
    fast."""
    v = str((env or {}).get("MCOMP_BROWSER_PACING") or "").strip().lower()
    return v if v in MODES else DEFAULT


def rng(seed=None):
    return random.Random(seed)


def step_parts(st):
    """(tool, args) of one run_steps step in any of its forms ({tool, args}, {tool, fields...}, {tool_name: args}),
    or (None, None). The args dict is the one the driver reads."""
    if not isinstance(st, dict):
        return None, None
    if "tool" in st:
        a = st.get("args")
        return st.get("tool"), (a if isinstance(a, dict) else st)
    for k, v in st.items():
        if k not in STEP_KEYS and isinstance(v, dict):
            return k, v
    return None, None


def copy_step(st):
    """A copy of a step and of its args dict (the caller's step is never changed while a plan is tried)."""
    tool, a = step_parts(st)
    new = dict(st)
    if a is not st:
        key = "args" if "tool" in st else tool
        new[key] = dict(a)
    return new


def own_delay(a):
    return isinstance(a, dict) and "delay_ms" in a


def split_runs(text, r, runs=None):
    """The text in 1-3 runs, split at random places between two ASCII characters (never inside a character
    sequence such as an accent or an emoji, never between CR and LF), each run at least RUN_MIN characters."""
    n = len(text)
    if runs is None:
        runs = 1 if n < RUN_MIN_CHARS else (2 if n < 3 * RUN_MIN_CHARS else r.randint(2, MAX_RUNS))
    if runs <= 1:
        return [text]
    ok = [i for i in range(RUN_MIN, n - RUN_MIN + 1)
          if ord(text[i - 1]) < 128 and ord(text[i]) < 128 and text[i - 1:i + 1] != "\r\n"]
    cuts = []
    for i in r.sample(ok, len(ok)):
        if all(abs(i - c) >= RUN_MIN for c in cuts):
            cuts.append(i)
            if len(cuts) == runs - 1:
                break
    cuts.sort()
    return [text[a:b] for a, b in zip([0] + cuts, cuts + [n])]


def path(p0, p1, r):
    """Points from p0 toward p1 for the pointer (not p1 itself: the click lands there): 2-4 points on a gentle curve,
    the last short of the target, each inside the box p0 and p1 span (so on screen), as integers."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if not math.isfinite(dist) or dist < PATH_MIN_PX:
        return []
    n = max(2, min(4, int(round(dist / 300)) + 2))
    bend = r.uniform(0.08, 0.22) * r.choice((-1, 1))
    cx, cy = (x0 + x1) / 2 - dy * bend, (y0 + y1) / 2 + dx * bend
    lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    pts = []
    for k in range(1, n + 1):
        t = (k / n) * r.uniform(0.86, 0.95)
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1 + r.uniform(-2, 2)
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1 + r.uniform(-2, 2)
        pts.append((int(round(min(max(x, lo_x), hi_x))), int(round(min(max(y, lo_y), hi_y)))))
    return pts


def pace_type(args, r, cap_s=CAP_S):
    """A type_text call aimed at a browser: the driver's pause between characters, unless the call sets its own.
    Changes args; returns a plan of counts, or None when nothing changed."""
    text = args.get("text")
    if own_delay(args) or not isinstance(text, str) or not text:
        return None
    d = min(r.randint(*KEY_MS), int(cap_s * 1000 // len(text)))
    if d <= 0:
        return None
    args["delay_ms"] = d
    return {"tool": "type_text", "chars": len(text), "runs": 1, "key_ms": [d, d], "step_ms": None, "moves": 0,
            "added_s": round(len(text) * d / 1000, 1)}


def _plan(steps, browser, targets, r, split, moves_on):
    """One try: (new steps, [(characters, the step that types them)], extra type steps, pointer moves, whether a
    browser field changes). Steps it changes are copies."""
    out, typed, extra, moves, fields = [], [], 0, 0, False
    for i, st in enumerate(steps):
        tool, a = step_parts(st)
        if i not in browser or tool is None:
            out.append(st)
            continue
        fields = fields or tool in FIELD_TOOLS
        if moves_on and tool in CLICK_TOOLS and i in targets:
            for x, y in path(targets[i][0], targets[i][1], r):
                out.append({"tool": "move_cursor", "args": {"scope": "desktop", "x": x, "y": y}})
                moves += 1
        text = a.get("text") if tool == "type_text" else None
        if not isinstance(text, str) or not text or own_delay(a):
            out.append(st)
            continue
        # A step that clicks (x,y) or focuses (element_token) before typing would do it again for each run and move
        # the caret: those are typed in one run.
        whole = any(a.get(k) is not None for k in ("x", "y", "element_token"))
        runs = split_runs(text, r) if split and not whole else [text]
        if len(runs) == 1:
            new = copy_step(st)
            out.append(new)
            typed.append((len(text), new))
            continue
        base = {k: v for k, v in a.items() if k not in ("text", "delay_ms", "tool") + STEP_KEYS}
        for j, run in enumerate(runs):
            new = {"tool": "type_text", "args": dict(base, text=run)}
            if j == 0:
                new.update({k: st[k] for k in ("wait_for", "timeout_ms") if k in st})
            if j == len(runs) - 1 and "expect" in st:
                new["expect"] = st["expect"]
            out.append(new)
            typed.append((len(run), new))
        extra += len(runs) - 1
    return out, typed, extra, moves, fields


def pace_batch(args, browser, targets, r, cap_s=CAP_S, moves_on=True):
    """A run_steps batch: browser is the set of step indexes aimed at a browser window, targets maps a pixel click's
    index to (where the pointer is, the screen point the click lands on). Replaces args["steps"] with a paced list
    and sets args["delay_ms"] unless the caller set it. Returns a plan of counts, or None when nothing changed."""
    steps = args.get("steps")
    if not isinstance(steps, list) or not browser or len(steps) > MAX_STEPS:
        return None
    cap = min(cap_s, BATCH_LIMIT_S - BATCH_MARGIN_S - len(steps) * STEP_COST_S)
    if cap <= 0:
        return None
    for split, moves in ((True, moves_on), (False, moves_on), (False, False)):
        out, typed, extra, nmoves, fields = _plan(steps, browser, targets, r, split, moves)
        fixed = extra * RUN_COST_S + nmoves * MOVE_COST_S
        if len(out) <= MAX_STEPS and fixed <= cap / 2:
            break
    keys = [r.randint(*KEY_MS) for _ in typed]
    step_ms = r.randint(*STEP_MS) if fields and not own_delay(args) and len(out) > 1 else None

    def timed():
        return (sum(n * d for (n, _), d in zip(typed, keys)) + (len(out) - 1) * (step_ms or 0)) / 1000
    if fixed + timed() > cap:
        f = max(0.0, (cap - fixed) / timed())
        keys = [int(d * f) for d in keys]
        step_ms = int(step_ms * f) if step_ms is not None else None
    if not typed and not nmoves and step_ms is None:
        return None
    for (_, st), d in zip(typed, keys):
        step_parts(st)[1]["delay_ms"] = d
    args["steps"] = out
    if step_ms is not None:
        args["delay_ms"] = step_ms
    return {"tool": "run_steps", "chars": sum(n for n, _ in typed), "runs": len(typed),
            "key_ms": [min(keys), max(keys)] if keys else None, "step_ms": step_ms, "moves": nmoves,
            "extra_runs": extra, "added_s": round(fixed + timed(), 1)}


NOTE_PREFIX = "metacomputer hub (human pacing):"


def note_of(plan):
    """The note a result gets when the hub added steps to a batch (the driver numbers its steps as it got them), or
    None."""
    moves, runs = (plan or {}).get("moves") or 0, (plan or {}).get("extra_runs") or 0
    if not moves and not runs:
        return None
    what = [w for w in (f"{moves} pointer move(s) before pixel clicks" if moves else "",
                        f"{runs} more typing run(s)" if runs else "") if w]
    return (f"{NOTE_PREFIX} added {' and '.join(what)} to this batch (BROWSER_PACING=human), so the driver's step "
            "numbers are higher than yours from the first one added")


def describe(plan):
    """A hub.log line for a plan: counts and milliseconds only."""
    if not plan:
        return ""
    bits = [f"{plan['tool']}: {plan.get('chars', 0)} characters in {plan.get('runs', 0)} runs"]
    if plan.get("key_ms"):
        bits.append("{}-{} ms per key".format(*plan["key_ms"]))
    if plan.get("step_ms") is not None:
        bits.append(f"{plan['step_ms']} ms between steps")
    if plan.get("moves"):
        bits.append(f"{plan['moves']} pointer moves")
    bits.append(f"about {plan.get('added_s', 0)} s added")
    return "pacing " + ", ".join(bits)
