"""The hub's copy of Cua Driver's own call and batch normalisation (driver 0.53.0), so every guard, and pacing, reads a
call the way the driver will run it.

The driver accepts more than its schemas show: a run_steps step may be `{tool, args}`, `{tool, <fields beside it>}` or
the shorthand `{<tool>: args}`; step tool names have aliases (`type` is type_text, `set` is set_value, `press` is
press_key, or a click when it names an element); a key name can be the tool (`{"tool": "down"}`); `triple_click` is a
click with count 3; a one-key hotkey is a press_key, and a press_key of a printable symbol types it; and a string
where the schema wants a number or a boolean (`"pid": "4211"`, `"delay_ms": "200"`) is turned into one. A guard that
tested only the canonical forms could be walked past with the others (a `{"type": {...}}` step aimed at Chrome went
out as background input).

So the hub normalises once, as the call arrives (lib/mcp_hub.py on_tools_call), before any guard: a direct call's
top-level scalars are coerced against its schema, and a run_steps batch is rewritten into canonical steps
(`{"tool": NAME, "args": {...}}` plus its own wait_for, expect and timeout_ms), trailing reads folded into `observe`;
the old top-level names `type_text_chars` and `run_actions` become type_text and run_steps.
The hub then sends the driver that normalised call, not the original: the canonical forms are a fixed point of the
driver's own normalisation, so what the guards checked is exactly what runs. A step the driver would refuse (an
unknown tool, two actions, malformed arguments) is left as it came: the driver refuses the whole batch before
running any of it.

Mirrored from the driver's source for 0.53.0 (cua-driver-core: batch_tools.rs TOOL_ALIASES, KEY_TOOL_NAMES,
SYMBOL_KEY_NAMES, parse_step, parse_action, rewrite_step, read_step and the trailing-read loop of parse; tool.rs
RENAMED_TOOLS, the type_text_chars alias, coerce_string_scalars and normalize_delivery_mode_args). Every rewrite here
is one the driver makes itself, so the driver's own pass over the result changes nothing more. Argument rewrites no
guard reads (press_key and hotkey key spellings, set_value's number, scroll's dx/dy, zoom's region, invoke_menu's
path, set_config) are left to the driver, which makes them the same way on what the hub sends. tests/test_hub.py
names every alias here; check them again against a new driver's batch_tools.rs and tool.rs before updating
CUA_DRIVER_VERSION.
"""
import copy
import re

DRIVER_VERSION = "0.53.0"
BATCHABLE_TOOLS = ("click", "double_click", "right_click", "set_value", "type_text", "press_key", "hotkey", "scroll",
                   "drag", "move_cursor")
TOOL_ALIASES = {"press": "press_key", "key": "press_key", "keypress": "press_key", "type": "type_text",
                "set": "set_value", "hover": "move_cursor", "mouse_move": "move_cursor"}
KEY_TOOL_NAMES = ("return", "enter", "tab", "escape", "esc", "space", "backspace", "up", "down", "left", "right", "home",
                  "end", "pageup", "pagedown")
SYMBOL_KEY_NAMES = {"asterisk": "*", "star": "*", "plus": "+", "at": "@", "hash": "#", "numbersign": "#", "pound": "#",
                    "dollar": "$", "percent": "%", "caret": "^", "ampersand": "&", "exclamation": "!", "exclam": "!",
                    "question": "?", "questionmark": "?", "underscore": "_", "tilde": "~", "colon": ":", "pipe": "|",
                    "bar": "|", "less": "<", "lessthan": "<", "greater": ">", "greaterthan": ">", "parenleft": "(",
                    "leftparen": "(", "parenright": ")", "rightparen": ")", "braceleft": "{", "braceright": "}",
                    "quotedbl": '"', "doublequote": '"'}
SHIFTED_SYMBOLS = '*+@#$%^&!?_~:|<>(){}"'
PRESS_ELEMENT_KEYS = ("element_token", "element_index", "role", "name", "label", "x")
OBSERVATION_TOOLS = ("get_window_state", "get_desktop_state", "get_accessibility_tree", "get_browser_state",
                     "list_windows", "verify_state", "zoom")
STEP_KEYS = ("tool", "args", "wait_for", "expect", "timeout_ms")
TOOL_RENAMES = {"type_text_chars": "type_text", "run_actions": "run_steps"}   # dispatch's old names
SELECTOR_KEYS = ("role", "name", "label", "nth", "app", "window")
MAX_STEPS = 64                  # the driver refuses a batch of more items than this, before anything else
MAX_SCALAR = 64                 # a longer string isn't tried as a number: the driver refuses it as the wrong type
DIGITS = frozenset("0123456789")


class Refused(ValueError):
    """A call the driver refuses before running anything; the hub refuses it the same way, with this text."""


def _int_text(t):
    """The integer a string spells as Rust's i64/u64 parse reads it (an optional sign, then digits), or None. One
    linear scan, no regular expression."""
    body = t[1:] if t[:1] in ("+", "-") else t
    if not body or any(c not in DIGITS for c in body):
        return None
    n = int(t)
    return n if -(1 << 63) <= n < (1 << 64) else None


def _float_text(t):
    """The finite number a string spells as Rust's f64 parse reads a decimal (sign, digits, an optional point and
    digits, an optional exponent), or None. One linear scan; inf and nan aren't finite, so they stay strings."""
    i, n = 0, len(t)
    if i < n and t[i] in "+-":
        i += 1
    start = i
    while i < n and t[i] in DIGITS:
        i += 1
    whole = i - start
    frac = 0
    if i < n and t[i] == ".":
        i += 1
        f0 = i
        while i < n and t[i] in DIGITS:
            i += 1
        frac = i - f0
    if whole + frac == 0:
        return None
    if i < n and t[i] in "eE":
        i += 1
        if i < n and t[i] in "+-":
            i += 1
        e0 = i
        while i < n and t[i] in DIGITS:
            i += 1
        if i == e0:
            return None
    if i != n:
        return None
    f = float(t)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def schemas_of(tools):
    """{tool name: input schema} from a tools/list answer."""
    out = {}
    for t in tools or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("inputSchema"), dict):
            out[t["name"]] = t["inputSchema"]
    return out


def _types(prop):
    """The JSON types a property schema admits: its `type` (a string or a list) and each anyOf/oneOf branch's."""
    out = []

    def add(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(x for x in v if isinstance(x, str))
    if isinstance(prop, dict):
        add(prop.get("type"))
        for k in ("anyOf", "oneOf"):
            for branch in prop.get(k) or [] if isinstance(prop.get(k), list) else []:
                if isinstance(branch, dict):
                    add(branch.get("type"))
    return out


def coerce_scalars(schema, args):
    """The driver's coerce_string_scalars: a top-level string argument whose schema admits a boolean, integer or
    number but not a string becomes that value ("true"/"false", "200", "1.5"); anything else stays. In place."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not isinstance(args, dict):
        return args
    for name, value in list(args.items()):
        if not isinstance(value, str) or name not in props:
            continue
        types = _types(props[name])
        if not types or "string" in types:
            continue
        text = value.strip()
        if len(text) > MAX_SCALAR:
            continue                            # no number or boolean is this long: left for the driver to refuse
        new = None
        if "boolean" in types and text.lower() == "true":
            new = True
        elif "boolean" in types and text.lower() == "false":
            new = False
        elif "integer" in types:
            new = _int_text(text)
        elif "number" in types:
            new = _float_text(text)
        if new is not None:
            args[name] = new
    return args


def rewrite_step(name, args):
    """The driver's rewrite_step: triple_click, a key name as the tool, a one-key hotkey, a press_key of a printable
    symbol. Returns the tool name; changes args in place."""
    if not isinstance(args, dict):
        return name
    lower = name.lower()
    if lower == "triple_click":
        args.setdefault("count", 3)
        return "click"
    if lower in KEY_TOOL_NAMES:
        args.setdefault("key", lower)
        return "press_key"
    if name == "hotkey":
        keys, single = args.get("keys"), None
        if isinstance(keys, list) and len(keys) == 1 and isinstance(keys[0], str):
            single = keys[0]
        elif isinstance(keys, str) and "+" not in keys:
            single = keys
        if single is not None:
            del args["keys"]
            args["key"] = single
            name = "press_key"
    if name == "press_key":
        mods = args.get("modifiers")
        no_mods = not isinstance(mods, list) or not mods
        text = None
        key = args.get("key")
        if isinstance(key, str):
            k = key.strip()
            folded = re.sub(r"[_\- ]", "", k.lower())
            text = SYMBOL_KEY_NAMES.get(folded)
            if text is None and len(k) == 1 and k in SHIFTED_SYMBOLS:
                text = k
        if no_mods and text is not None:
            args.pop("key", None)
            args.pop("modifiers", None)
            args["text"] = text
            return "type_text"
    return name


def read_step(step):
    """The driver's read_step: a read (get_window_state, a screenshot, zoom, ...) written as a step, as (tool, args),
    or None."""
    if not isinstance(step, dict) or "wait_for" in step or "expect" in step:
        return None
    if "tool" in step:
        name, args = step.get("tool"), step.get("args")
        if not isinstance(name, str):
            return None
    else:
        keys = [k for k in step if k not in STEP_KEYS]
        if len(keys) != 1:
            return None
        name, args = keys[0], step.get(keys[0])
    if name in OBSERVATION_TOOLS:
        tool = name
    elif "screenshot" in name.lower():
        tool = "screenshot"
    else:
        return None
    return tool, (copy.deepcopy(args) if isinstance(args, dict) else {})


def step_action(step):
    """(name, raw args, ok) of a step's action as the driver's parse_step reads it: `{tool, args}`, flat fields beside
    `tool`, or the shorthand `{name: args}`. name None: a check-only step. ok False: a shape the driver refuses."""
    if not isinstance(step, dict):
        return None, None, False
    has_tool = "tool" in step
    short = [k for k, v in step.items() if k not in STEP_KEYS and v is not None
             and not (isinstance(v, dict) and not v and has_tool)]
    args_absent = step.get("args") is None
    if has_tool and short and args_absent:
        if not isinstance(step["tool"], str):
            return None, None, False
        return step["tool"], {k: step[k] for k in short}, True
    if has_tool and short:
        return None, None, False                    # fields beside `args`
    if has_tool:
        return (step["tool"], step.get("args"), True) if isinstance(step["tool"], str) else (None, None, False)
    if len(short) == 1:
        if "args" in step:
            return None, None, False
        return short[0], step.get(short[0]), True
    if not short:
        return (None, None, False) if "args" in step else (None, None, True)
    return None, None, False


def normalize_step(step, schemas):
    """One run_steps step in the driver's canonical form: `{"tool": NAME, "args": {...}}` plus the step's own wait_for,
    expect and timeout_ms. A check-only step, a read step and a step the driver would refuse come back as they were
    (a copy). Never changes the step it's given."""
    if read_step(step) is not None:
        return copy.deepcopy(step)
    name, raw, ok = step_action(step)
    if not ok or name is None:
        return copy.deepcopy(step)
    if raw is None:
        args = {}
    elif isinstance(raw, dict):
        args = copy.deepcopy(raw)
    else:
        return copy.deepcopy(step)
    if name == "press" and "key" not in args and any(k in args for k in PRESS_ELEMENT_KEYS):
        name = "click"
    else:
        name = TOOL_ALIASES.get(name, name)
    # The driver rewrites once per pass, and runs its pass again on what the hub sends: rewrite until nothing changes,
    # so the result is a fixed point (a key name as the tool with a symbol key ends as type_text, as a second pass
    # would make it). Each rewrite only moves toward press_key and type_text, so this ends within a few rounds.
    for _ in range(8):
        before = (name, repr(sorted(args.items(), key=lambda kv: kv[0])))
        name = rewrite_step(name, args)
        if (name, repr(sorted(args.items(), key=lambda kv: kv[0]))) == before:
            break
    if name not in BATCHABLE_TOOLS:
        return copy.deepcopy(step)                  # the driver refuses it, and with it the whole batch
    coerce_scalars(schemas.get(name) or {}, args)
    out = {"tool": name, "args": args}
    for k in ("wait_for", "expect", "timeout_ms"):
        if k in step:
            out[k] = copy.deepcopy(step[k])
    return out


def normalize_batch(args, schemas):
    """A run_steps call's arguments as the driver will run them (in place): its own scalars coerced, trailing reads
    folded into `observe` (the latest get_window_state or screenshot wins; an explicit observe's fields win over it),
    and every step in canonical form. Returns args."""
    if not isinstance(args, dict):
        return args
    coerce_scalars(schemas.get("run_steps") or {}, args)
    steps = args.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        # The driver's first check, on the list as sent (before it folds trailing reads): refused the same way.
        raise Refused(f"`steps` must hold 1 to {MAX_STEPS} items, got "
                      f"{len(steps) if isinstance(steps, list) else 'none'}")
    reads = [read_step(st) for st in steps]
    last_act = max((i for i, rd in enumerate(reads) if rd is None), default=-1)
    trailing, zooms = None, []
    if last_act >= 0:
        # Reads after the last action, latest first, as the driver's loop takes them (one pass).
        for i in range(len(steps) - 1, last_act, -1):
            tool, rargs = reads[i]
            if tool in ("get_window_state", "screenshot"):
                if trailing is None:                # the latest trailing read wins
                    trailing = {"include_screenshot": True} if tool == "screenshot" else rargs
            elif tool == "zoom":
                zooms.insert(0, steps[i])           # kept at the end: the driver runs them after the observation
            # other trailing reads add nothing the observation doesn't show: the driver drops them too
        steps = list(steps[:last_act + 1]) + zooms
    else:
        steps = list(steps)
    if trailing is not None:
        explicit = args.get("observe")
        if explicit is None or explicit is False:
            args["observe"] = trailing
        elif isinstance(explicit, dict):
            args["observe"] = dict(trailing, **explicit)
        else:
            args["observe"] = trailing
    args["steps"] = [normalize_step(s, schemas) for s in steps]
    return args


def tool_name(name):
    """A top-level tool's current name: the driver's dispatch also runs `type_text_chars` (type_text) and
    `run_actions` (run_steps)."""
    return TOOL_RENAMES.get(name, name) if isinstance(name, str) else name


def normalize_call(name, args, schemas):
    """A direct call as the driver's dispatch reads it (in place): its top-level scalars coerced against its schema,
    and `dispatch` (an old spelling of delivery_mode) or any delivery_mode value read as "foreground" or
    "background", for a tool that takes one. A run_steps call goes through normalize_batch instead."""
    if not isinstance(args, dict):
        return args
    if name == "run_steps":
        return normalize_batch(args, schemas)
    schema = schemas.get(name) or {}
    coerce_scalars(schema, args)
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if isinstance(props.get("delivery_mode"), dict) and ("delivery_mode" in args or "dispatch" in args):
        v = args["delivery_mode"] if "delivery_mode" in args else args["dispatch"]
        args.pop("dispatch", None)
        args["delivery_mode"] = "foreground" if isinstance(v, str) and v.lower() == "foreground" else "background"
    return args
