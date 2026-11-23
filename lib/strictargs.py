"""The command-line rules every metacomputer program follows, for its Python programs (scripts/strictargs.sh is the
same for its bash ones; bin/metacomputer's grammar and the vault's declarations, vault/ArgSpecs.swift, follow them too).

Before a program does anything, its command line is checked; anything it wouldn't use is a usage error (exit 2):
  - a request for usage: exactly -h or --help prints the usage and exits 0 (nothing else is done); anything shaped like
    one (-hx, --help=x, --h, --he, --hel) is refused, as is a request for usage given as an option's value;
  - an option it doesn't have, or an abbreviated one (--jso for --json);
  - an option given twice (a flag, or one that takes a value; options meant to repeat say so);
  - a value that starts with '-' where no value can (a name, a path, a number of seconds), as `--x VALUE` or
    `--x=VALUE`: it's an option typed in the wrong place, never a value (a negative number is a value);
  - a word beyond its arguments, or one missing.
`--` ends the options only where the program says it does (what follows is another program's command).

Two ways to declare a command line:

    ap = strictargs.Parser(prog="metacomputer thing")      # argparse, with the rules above
    ...                                                     # (action="text": a text that may start with '-')
    a = ap.parse_args(argv)

    strictargs.expect(argv, ["view --port PORT [--json]", "clip [--clear-after SECONDS]"], prog="liveview.py")

expect() takes the forms a hand-parsed program accepts and checks argv against them before the program reads it its own
way. In a form: a word in lowercase (or an option) is itself, `a|b` is either, an UPPERCASE word is a value that never
starts with '-', `~TEXT` one that may (a text, JSON, a number) but is never a request for usage, `[...]` is optional,
`X...` takes one or more, `[X...]` none or more, and `-- ~ARGS...` takes the rest whole. Options (--x, --x VALUE) may
come in any order before a `--`, each once; the other words in the form's order.
(A placeholder right after an option is that option's value: a flag a form requires goes elsewhere, or in [].)
"""
import argparse
import os
import re
import sys

HELP = ("-h", "--help")
# MCOMP_PARSE_ONLY=1 (tests/test_help.py's valid forms): once a command line is taken, say so and end the run doing
# nothing else, so a test can show a program's real arguments aren't refused without the program acting on them. It
# ends with PARSE_ONLY_EXIT, never 0: a guard or check run this way (an inherited environment, an agent setting it)
# refuses, so whatever it guards never goes ahead.
PARSE_ONLY = os.environ.get("MCOMP_PARSE_ONLY") == "1"
PARSE_ONLY_EXIT = 99                    # "parse only: nothing was done" (scripts/strictargs.sh, the vault: the same)
# MCOMP_PARSE_ONLY=describe: the program's declaration itself (every subcommand's options and words, or its forms), as
# JSON, before anything is read or done (tests/test_help.py makes a valid command line for each from it).
DESCRIBE = os.environ.get("MCOMP_PARSE_ONLY") == "describe"


def _tree(p):
    out = {"options": [], "positionals": [], "subs": {}}
    for act in p._actions:
        if isinstance(act, argparse._HelpAction):
            continue
        if isinstance(act, argparse._SubParsersAction):
            for name, sp in act.choices.items():
                out["subs"][name] = _tree(sp)
            continue
        choices = list(act.choices) if act.choices is not None else None
        nargs = act.nargs if isinstance(act.nargs, (int, type(None))) else str(act.nargs)
        entry = {"nargs": nargs, "choices": choices, "int": act.type in (int, float)}
        if act.option_strings:
            entry.update(opt=act.option_strings[-1], required=bool(act.required), list=isinstance(act, argparse._AppendAction))
            out["options"].append(entry)
        else:
            out["positionals"].append(entry)
    out["groups"] = [[g._group_actions[0].option_strings[-1]] + ([] if g._group_actions[0].nargs == 0 else ["x"])
                     for g in p._mutually_exclusive_groups if g.required and g._group_actions]
    return out
NUMBER = re.compile(r"-\d+(\.\d+)?[a-z]*$")


def parsed(prog, info=None):
    """A program's command line is taken: under MCOMP_PARSE_ONLY, the end of its run (exit 99, nothing done), saying
    what the form it took declares (INFO: its flags, its options that take a value, how many words it has and may
    have), from which tests/test_help.py makes that form's misuse rows."""
    if PARSE_ONLY:
        import json
        print(f"parse-only: {prog}: taken " + json.dumps(info or {}))
        print("parse only: nothing was done", file=sys.stderr)
        sys.exit(PARSE_ONLY_EXIT)


def _describe(parser, ns):
    """The options and words of the (sub)commands NS went through, for parsed()."""
    flags, values, repeatable, free, used, most, variadic, path = [], [], [], [], 0, 0, False, []

    def walk(p):
        nonlocal used, most, variadic
        for act in p._actions:
            if isinstance(act, argparse._HelpAction):
                continue
            if isinstance(act, argparse._SubParsersAction):
                name = getattr(ns, act.dest, None)
                if name in act.choices:
                    path.append(name)
                    used += 1
                    most += 1
                    walk(act.choices[name])
                continue
            if act.option_strings:
                (flags if act.nargs == 0 else values).append(act.option_strings[-1])
                if isinstance(act, argparse._AppendAction):         # meant to repeat
                    repeatable.append(act.option_strings[-1])
                if isinstance(act, _OnceText):                      # a text, which may start with '-'
                    free.append(act.option_strings[-1])
                continue
            v = getattr(ns, act.dest, None)
            used += len(v) if isinstance(v, list) else (0 if v is None else 1)
            if act.nargs in ("*", "+", argparse.REMAINDER):
                variadic = True
            else:
                most += act.nargs if isinstance(act.nargs, int) else 1
    walk(parser)

    def leaves(p, pre):
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            return [pre]
        return [x for a in subs for name, sp in a.choices.items() for x in leaves(sp, pre + [name])]
    return {"path": path, "flags": flags, "values": values, "repeatable": repeatable, "free": free, "words": used,
            "max_words": None if variadic else most, "all": [" ".join(x) for x in leaves(parser, [])]}


def is_help_word(s):
    """A request for usage, or a word shaped like one: -h, --help, -hx, --help=x, --h, --he, --hel."""
    return isinstance(s, str) and (s in HELP or s in ("--h", "--he", "--hel") or s.startswith("--help=")
                                   or (s.startswith("-h") and not s.startswith("--")))


def _value_ok(parser, values, option_string):
    """An option's value (never a positional's: a `--`'s rest is another program's words) is never a request for usage,
    nor an option (`--out=--bogus`), but a negative number may be one (-1, -1.5, -30m)."""
    if option_string is None:
        return
    for v in values if isinstance(values, list) else [values]:
        if is_help_word(v):
            parser.error(f"{option_string} needs a value, not a request for usage")
        if isinstance(v, str) and v.startswith("-") and v != "-" and not NUMBER.match(v):
            parser.error(f"{option_string} needs a value, not an option ('{v[:40]}'; a file named so is ./{v[:40]})")


class _Once(argparse._StoreAction):
    """store, given at most once, never a request for usage or an option as its value."""

    def __call__(self, parser, namespace, values, option_string=None):
        if option_string is not None:
            _once(parser, namespace, self.dest, option_string)
        _value_ok(parser, values, option_string)
        super().__call__(parser, namespace, values, option_string)


class _OnceText(_Once):
    """action="text": a text (a description, a label: `--overridden-by "--vm demo"`), given at most once, never a
    request for usage, but it may start with '-'."""

    def __call__(self, parser, namespace, values, option_string=None):
        if option_string is not None:
            _once(parser, namespace, self.dest, option_string)
        if is_help_word(values):
            parser.error(f"{option_string} needs a value, not a request for usage")
        argparse._StoreAction.__call__(self, parser, namespace, values, option_string)


def _once(parser, namespace, dest, option_string):
    seen = namespace.__dict__.setdefault("_strictargs_seen", set())
    if dest in seen:
        parser.error(f"{option_string or dest} given twice (once, please)")
    seen.add(dest)


class _OnceConst(argparse._StoreConstAction):
    """A flag (store_true, store_false, store_const), given at most once."""

    def __call__(self, parser, namespace, values, option_string=None):
        _once(parser, namespace, self.dest, option_string)
        super().__call__(parser, namespace, values, option_string)


class _OnceTrue(_OnceConst):
    def __init__(self, option_strings, dest, default=False, required=False, help=None, deprecated=False):
        super().__init__(option_strings=option_strings, dest=dest, const=True, default=default, required=required,
                         help=help)


class _OnceFalse(_OnceConst):
    def __init__(self, option_strings, dest, default=True, required=False, help=None, deprecated=False):
        super().__init__(option_strings=option_strings, dest=dest, const=False, default=default, required=required,
                         help=help)


class _Append(argparse._AppendAction):
    """append (an option meant to repeat), never a request for usage."""

    def __call__(self, parser, namespace, values, option_string=None):
        _value_ok(parser, values, option_string)
        super().__call__(parser, namespace, values, option_string)


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **k):
        k.setdefault("allow_abbrev", False)
        super().__init__(*a, **k)
        self.register("action", None, _Once)
        self.register("action", "store", _Once)
        self.register("action", "store_true", _OnceTrue)
        self.register("action", "store_false", _OnceFalse)
        self.register("action", "store_const", _OnceConst)
        self.register("action", "append", _Append)
        self.register("action", "text", _OnceText)

    def parse_known_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        for i, a in enumerate(args):
            if a == "--":
                break
            # A word shaped like a request for usage that isn't one (exactly -h or --help) is refused here: argparse
            # would read -hx as -h and x, and a positional could take --help=x as a value.
            if is_help_word(a) and a not in HELP:
                self.error(f"'{a[:40]}' isn't a request for usage: -h or --help")
        ns, rest = super().parse_known_args(args, namespace)
        ns.__dict__.pop("_strictargs_seen", None)
        return ns, rest

    def parse_args(self, args=None, namespace=None):
        if DESCRIBE:
            import json
            print("parse-only-describe: " + json.dumps(_tree(self)))
            sys.exit(PARSE_ONLY_EXIT)
        ns = super().parse_args(args, namespace)
        if PARSE_ONLY:                          # (subcommands' parsers go through parse_known_args, never here)
            parsed(self.prog, _describe(self, ns))
        return ns


# --- expect(): the forms of a hand-parsed command line ------------------------------------------------------------

class _Form:
    def __init__(self, text):
        self.text = text
        self.options = {}          # --name -> (placeholder or None, required)
        self.words = []            # (kind, value, optional, many): kind lit|alt|ph|any|rest
        toks = text.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            optional = t.startswith("[")
            body = t.lstrip("[")
            closes = body.endswith("]")
            body = body.rstrip("]")
            if body.startswith("--") and body != "--":
                # --name, --name VALUE, [--name], [--name VALUE]: an option, in any order
                nxt = toks[i + 1].rstrip("]") if i + 1 < len(toks) else ""
                takes = (not optional or not closes) and nxt[:1].isupper() or nxt.startswith("~")
                if takes and (not optional or toks[i + 1].endswith("]")):
                    self.options[body] = (nxt, not optional)
                    i += 2
                else:
                    self.options[body] = (None, not optional)
                    i += 1
                continue
            many = body.endswith("...")
            body = body[:-3] if many else body
            if body == "--":
                self.words.append(("lit", "--", optional, False))
            elif body.startswith("~"):
                rest = many and bool(self.words) and self.words[-1][1] == "--"
                self.words.append(("rest" if rest else "any", body, optional, many))
            elif body[:1].isupper():
                self.words.append(("ph", body, optional, many))
            elif "|" in body:
                self.words.append(("alt", tuple(body.split("|")), optional, many))
            else:
                self.words.append(("lit", body, optional, many))
            i += 1

    def describe(self, n, words, total=1):
        """For parsed(): which form (of TOTAL), its flags and value options, how many words it took and may take."""
        many = any(m for _k, _v, _o, m in self.words)
        return {"form": n, "forms": total, "flags": [o for o, (ph, _r) in self.options.items() if ph is None],
                "values": [o for o, (ph, _r) in self.options.items() if ph is not None],
                "free": [o for o, (ph, _r) in self.options.items() if ph is not None and ph.startswith("~")],
                "words": len(words), "max_words": None if many else sum(1 for k, v, _o, _m in self.words if v != "--")}

    def match(self, argv):
        """(None, options, words) when argv is this form (options by name: its value, or True; the words without a
        `--` the form takes), otherwise (why not, None, None)."""
        seen, words, i, opts = set(), [], 0, {}
        dashdash = any(w[1] == "--" for w in self.words)
        while i < len(argv):
            a = argv[i]
            if a == "--" and dashdash:
                words.append(a)
                words.extend(argv[i + 1:])
                break
            name = a.split("=", 1)[0] if a.startswith("--") else a
            if a.startswith("--") and name in self.options:
                ph, _req = self.options[name]
                if name in seen:
                    return f"{name} given twice", None, None
                seen.add(name)
                if ph is None:
                    if "=" in a:
                        return f"{name} takes no value", None, None
                    opts[name] = True
                    i += 1
                    continue
                if "=" in a:
                    v = a.split("=", 1)[1]
                    i += 1
                else:
                    if i + 1 >= len(argv):
                        return f"{name} needs a value", None, None
                    v = argv[i + 1]
                    i += 2
                why = _value_problem(ph, v)
                if why:
                    return f"{name} {why}", None, None
                opts[name] = v
                continue
            if is_help_word(a):
                return "a request for usage is -h or --help, on its own", None, None
            words.append(a)
            i += 1
        for name, (_ph, req) in self.options.items():
            if req and name not in seen:
                return f"{name} is needed", None, None
        why = _match_words(self.words, words)
        if why:
            dash = [w for w in words if w.startswith("-") and w not in ("-", "--")]
            return (f"unknown option '{dash[0][:40]}'" if dash else why), None, None
        if dashdash and "--" in words:
            k = words.index("--")
            words = words[:k] + words[k + 1:]
        return None, opts, words


def _value_problem(ph, v):
    if is_help_word(v):
        return "needs a value, not a request for usage"
    if not ph.startswith("~") and v.startswith("-") and v != "-":
        return f"needs a value ({ph}), not an option ('{v[:40]}')"
    return None


def _match_words(pattern, words):
    def one(p, w):
        kind, val, _opt, _many = p
        if kind == "lit":
            return w == val
        if kind == "alt":
            return w in val
        if kind == "ph":
            return not (w.startswith("-") and w != "-") and not is_help_word(w)
        return not is_help_word(w)                    # any, rest

    def go(pi, wi):
        if pi == len(pattern):
            return wi == len(words)
        kind, val, opt, many = pattern[pi]
        if kind == "rest":
            return all(True for _ in words[wi:])      # the rest, whole (another program's words)
        if many:
            n = 0
            while wi + n < len(words) and one(pattern[pi], words[wi + n]):
                n += 1
                if go(pi + 1, wi + n):
                    return True
            return opt and go(pi + 1, wi)
        if wi < len(words) and one(pattern[pi], words[wi]) and go(pi + 1, wi + 1):
            return True
        return opt and go(pi + 1, wi)
    return None if go(0, 0) else "its words aren't one of its forms"


class Parsed:
    def __init__(self, form, opts, words, info=None):
        self.form, self.opts, self.words, self.info = form, opts, words, info    # info: what parsed() says of it


def expect(argv, forms, prog, usage=None, defer=False):
    """argv against FORMS (see the module's notes), before a hand-parsed program reads it: exactly -h or --help (before
    a `--` a form takes) prints the usage and exits 0; anything else no form takes is a usage error (exit 2). Returns
    which form it is (.form, its index), its options (.opts: name -> value, or True for a flag) and its words (.words,
    without the `--`), for the program to read instead of argv. defer=True: the program calls parsed(prog, .info) itself, once
    its own checks of what it read are done too."""
    forms = [forms] if isinstance(forms, str) else list(forms)
    if DESCRIBE:
        import json
        print("parse-only-describe: " + json.dumps({"forms": forms}))
        sys.exit(PARSE_ONLY_EXIT)
    short = "usage: " + "\n       ".join(f"{prog} {f}".rstrip() for f in forms)
    text = usage or short
    head = argv[:argv.index("--")] if "--" in argv and any(" -- " in f" {f} " for f in forms) else argv
    if any(a in HELP for a in head):
        print(text)
        sys.exit(0)
    why = None
    for n, f in enumerate(forms):
        form = _Form(f)
        w, opts, words = form.match(list(argv))
        if w is None:
            info = form.describe(n, words, len(forms))
            if not defer:
                parsed(prog, info)
            return Parsed(n, opts, words, info)
        why = why or w
    # Every form starting with a command word (not an option, a value or something optional): say which is wrong.
    heads = [f.split()[0] for f in forms if f.split()]
    if heads and all(h[:1].islower() for h in heads):
        first = argv[0] if argv else ""
        if not any(first in h.split("|") for h in heads):
            why = f"not one of its commands ({', '.join(heads)})" if argv else "a command is needed"
    print(f"{prog}: {why} (nothing was run)\n{short}", file=sys.stderr)
    sys.exit(2)
