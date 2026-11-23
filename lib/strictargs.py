"""argparse that refuses what a command wouldn't use, for metacomputer's own command lines.

    import strictargs
    ap = strictargs.Parser(prog="metacomputer thing")

On top of what argparse refuses already (an option it doesn't know, a word beyond the arguments), a Parser refuses
a second value for an option that takes one (argparse would keep the last, silently; options meant to repeat use
action="append"), and an abbreviated option (`--jso` for `--json`, `--hel` for `--help`): either is a usage error,
before the command does anything. Subcommands' parsers are Parsers too.
"""
import argparse


class _Once(argparse._StoreAction):
    """store, given at most once."""

    def __call__(self, parser, namespace, values, option_string=None):
        seen = namespace.__dict__.setdefault("_strictargs_seen", set())
        if self.dest in seen:
            parser.error(f"{option_string or self.dest} given twice (one value, please)")
        seen.add(self.dest)
        super().__call__(parser, namespace, values, option_string)


class Parser(argparse.ArgumentParser):
    def __init__(self, *a, **k):
        k.setdefault("allow_abbrev", False)
        super().__init__(*a, **k)
        self.register("action", None, _Once)
        self.register("action", "store", _Once)

    def parse_known_args(self, args=None, namespace=None):
        ns, rest = super().parse_known_args(args, namespace)
        ns.__dict__.pop("_strictargs_seen", None)
        return ns, rest
