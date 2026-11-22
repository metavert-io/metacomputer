#!/usr/bin/env python3
"""Where metacomputer's computers are, and which one a request means: one resolver for the CLI (--vm, --on), the
listings and anything else that has to turn "the one on my Mac" into a computer.

Every computer has a name and a place. The places: `mac`, this Mac, and the providers' places, which `cloud` stands
for as a group. Every computer here is on this Mac: no provider is registered (lib/cloud/ has the
providers' interface, with this Mac's own adapter and a stand-in for tests), so PROVIDERS is empty and `cloud` has
no computers.

resolve(names_and_places, said, default) picks a computer from (name, place) pairs, in this order:
  1. a computer's name: that computer;
  2. a provider's name: its one computer; with several there, the default if it's one of them, else refused,
     listing them;
  3. a place (mac, cloud): the default if it's there, else the one computer there, else refused, listing them;
  4. nothing: the default (with none, the one computer, else refused, listing them);
  5. a place with no computer, or a word that's none of these: refused, saying what there is.
Refusals are Unclear (more than one fits: say which) or NotFound (none does), with words for the person; nothing is
ever guessed. resolve_flags() is the CLI's form: --on takes a place, and with --vm too both must agree.

    placement.py resolve [--vm NAME] [--on PLACE] [--default NAME] --computer NAME=PLACE ...
        the computer's name on stdout; a refusal on stderr, exit 1 (bin/metacomputer's --on)
    placement.py places [--json]
        the places, and the providers that make up cloud
    placement.py name-ok NAME
        exit 1, saying why, when NAME is a place's name (mac, cloud, a provider's): `vm create` refuses those
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud  # noqa: E402

MAC = "mac"
CLOUD = "cloud"
PLACES = {MAC: "this Mac"}
# The providers' places, which `cloud` is the group of: place -> how it's shown, from the registered adapters
# (lib/cloud providers(): neither this Mac's nor the tests' stand-in). There are none.
PROVIDERS = cloud.providers()
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
NO_CLOUD = "this metacomputer has no cloud computers set up; every computer it has is on this Mac (metacomputer list)"


class Refused(ValueError):
    pass


class Unclear(Refused):
    """More than one computer fits what was said."""


class NotFound(Refused):
    """No computer fits what was said."""


def label(place, providers=None):
    """How a place is shown: "this Mac", a provider's own name, or "the cloud"."""
    providers = PROVIDERS if providers is None else providers
    if place in PLACES:
        return PLACES[place]
    if place == CLOUD:
        return "the cloud"
    return providers.get(place, place)


def on_place(place, providers=None):
    """"on this Mac", "in the cloud", "on <provider>"."""
    return "in the cloud" if place == CLOUD else "on " + label(place, providers)


def is_place(word, providers=None):
    providers = PROVIDERS if providers is None else providers
    return word in (MAC, CLOUD) or word in providers


def at(place, where):
    """Is a computer whose place is `place` at `where` (mac, cloud or a provider's name)? cloud is any place but mac."""
    return place != MAC if where == CLOUD else place == where


def computers(names_and_places):
    """{name: place} from (name, place) pairs; the first pair for a name counts."""
    out = {}
    for name, place in names_and_places:
        if isinstance(name, str) and isinstance(place, str) and name:
            out.setdefault(name, place)
    return out


def _listing(names):
    return ", ".join(sorted(names))


def nothing_at(word, providers=None):
    """What to say when no computer is at `word`: a place with none there, or a word that's no place."""
    providers = PROVIDERS if providers is None else providers
    w = (word or "").strip().lower()
    if w == MAC:
        return "there's no computer on this Mac yet (metacomputer setup makes one)"
    if w == CLOUD:
        return NO_CLOUD
    if w in providers:
        return f"there's no computer {on_place(w, providers)} yet"
    if not providers:
        return (f"this metacomputer has no cloud computers set up, so nothing is on {word!r}; every computer it has "
                "is on this Mac (metacomputer list)")
    return f"there's no place called {word!r} here (the places: {', '.join([MAC, CLOUD, *sorted(providers)])})"


def _pick(there, default, where, providers):
    """The default if it's among `there`, else the only one; refused when there are none or several."""
    if default in there:
        return default
    if len(there) == 1:
        return there[0]
    if there:
        why = "your default computer isn't one of them" if default else "there's no default computer"
        raise Unclear(f"more than one computer is {on_place(where, providers)} ({_listing(there)}) and {why}: say "
                      "which, with --vm NAME")
    raise NotFound(nothing_at(where, providers))


def place_name(name, providers=None):
    """The reason a computer can't be called `name` (it's a place's: mac, cloud or a provider's), else None."""
    providers = PROVIDERS if providers is None else providers
    if isinstance(name, str) and is_place(name.strip().lower(), providers):
        return (f"{name!r} is a place's name ({', '.join([MAC, CLOUD, *sorted(providers)])}), so it can't be a "
                "computer's: --on would mean the place, and a request could mean either")
    return None


def _check_default(comps, default):
    if default is not None and default not in comps:
        raise NotFound(f"your default computer, {default}, isn't one of the computers here "
                       f"({_listing(comps) or 'none'}); metacomputer default NAME chooses another")


def resolve(names_and_places, said=None, default=None, providers=None):
    """The computer `said` means (module docstring); Unclear or NotFound when that isn't one computer. A default
    that isn't one of the computers refuses wherever it would count: it's never replaced by another computer. A
    computer whose name is a place's (made by hand: vm create refuses such names) is never taken for the place:
    saying that word is refused as unclear."""
    providers = PROVIDERS if providers is None else providers
    comps = computers(names_and_places)
    word = (said or "").strip()
    if word and word in comps:
        if place_name(word, providers):
            raise Unclear(f"{word!r} is both a computer's name and a place: for the computer, --vm {word}; for the "
                          f"place, --on {word.lower()}")
        return word
    _check_default(comps, default)
    if not word:
        if default is not None:
            return default
        if len(comps) == 1:
            return next(iter(comps))
        if not comps:
            raise NotFound("there's no computer yet (metacomputer setup makes one)")
        raise Unclear(f"there's more than one computer ({_listing(comps)}) and no default: say which, with --vm NAME "
                      "(metacomputer default NAME makes one the default)")
    w = word.lower()
    if not is_place(w, providers):
        others = sorted({p for p in comps.values() if p != MAC})
        if not others:
            raise NotFound(f"nothing here is called {word!r}, and {NO_CLOUD}")
        raise NotFound(f"nothing here is called {word!r}: the computers are {_listing(comps)}, and the places "
                       f"{', '.join([MAC, CLOUD] + others)}")
    return _pick(sorted(n for n, p in comps.items() if at(p, w)), default, w, providers)


def resolve_flags(names_and_places, vm=None, on=None, default=None, providers=None):
    """bin/metacomputer's --vm NAME and --on PLACE: --on takes a place (a computer's name goes with --vm), and with a
    VM named too, that VM must be at the place. The computer's name, or Refused."""
    providers = PROVIDERS if providers is None else providers
    comps = computers(names_and_places)
    where = None
    if on is not None:
        where = on.strip().lower()
        if not where:
            raise Refused("--on needs a place: mac (this Mac) or cloud")
        if not is_place(where, providers):
            if on in comps:
                raise Refused(f"--on takes a place (mac or cloud), not a computer's name: for the computer {on}, "
                              f"use --vm {on}")
            raise NotFound(nothing_at(on, providers))
    if vm:
        if vm not in comps:
            raise NotFound(f"this checkout has no computer named {vm} (metacomputer list)")
        if where is not None and not at(comps[vm], where):
            extra = f", and {NO_CLOUD}" if where != MAC and not any(p != MAC for p in comps.values()) else ""
            raise Refused(f"--vm {vm} and --on {on} disagree: {vm} is on {label(comps[vm], providers)}{extra}")
        return vm
    if where is None:
        return resolve(names_and_places, None, default, providers)
    # A place, matched against places only: a computer named like the place (cloud, mac) is never taken for it.
    _check_default(comps, default)
    return _pick(sorted(n for n, p in comps.items() if at(p, where)), default, where, providers)


def _pairs(specs):
    out = []
    for s in specs or []:
        name, sep, place = s.partition("=")
        if not sep or not NAME.fullmatch(name) or not place:
            raise Refused(f"--computer takes NAME=PLACE, not {s!r}")
        out.append((name, place))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="placement.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("resolve")
    p.add_argument("--vm")
    p.add_argument("--on")
    p.add_argument("--default")
    p.add_argument("--computer", action="append", default=[])
    p = sub.add_parser("places")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("name-ok")
    p.add_argument("name")
    a = ap.parse_args(argv)
    if a.cmd == "name-ok":
        why = place_name(a.name)
        if why:
            print(why)
            return 1
        return 0
    if a.cmd == "places":
        places = [{"place": MAC, "label": PLACES[MAC], "group": None}]
        places += [{"place": k, "label": v, "group": CLOUD} for k, v in sorted(PROVIDERS.items())]
        if a.json:
            print(json.dumps({"places": places, "cloud": sorted(PROVIDERS)}))
        else:
            for pl in places:
                print(f"{pl['place']:<12} {pl['label']}")
            if not PROVIDERS:
                print(f"{CLOUD:<12} (none: {NO_CLOUD})")
        return 0
    try:
        print(resolve_flags(_pairs(a.computer), vm=a.vm, on=a.on, default=a.default or None))
    except Refused as e:
        print(f"metacomputer: {e}; nothing was done", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
