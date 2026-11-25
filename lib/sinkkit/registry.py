"""The kinds of event sink metacomputer sends to: an explicit list, so adding one is its folder plus one reviewed line
here.

Each kind is a folder sinks/<kind>/ at the top of the repository (sinks/README.md says what one holds): its manifest
(manifest.json: its settings, what it carries, its docs, its owners; lib/sinkkit/manifests.py), its code (sink.py: the
rules for its URL, its format and how it's delivered, which lib/events.py calls), its page for contributors (README.md)
and its cases (tests/). Their order here is the order they're offered in (`metacomputer events sink add --kind`, its
messages, docs/sinks.md, the vault's own list).

A kind's code is loaded from its folder under the private package name `mcomp_sinks` (mcomp_sinks.<kind>.sink), never
by putting the repository on sys.path, where another installed package of the same name could stand in for it. Works
under any python3 (macOS's 3.9 included)."""
import importlib.util
import json
import os
import sys
import threading
import types

SINKS = (
    "webhook",
    "otlp",
    "syslog",
    "ntfy",
)

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sinks")
PACKAGE = "mcomp_sinks"
_JSON = {}
_LOCK = threading.RLock()           # one thread loads a kind's code; another waits for it, never sees it half-made


def folder(kind):
    """sinks/<kind>, for a registered kind; KeyError otherwise."""
    if kind not in SINKS:
        raise KeyError(kind)
    return os.path.join(ROOT, kind)


def data(kind):
    """sinks/<kind>/manifest.json, read once."""
    if kind not in _JSON:
        with open(os.path.join(folder(kind), "manifest.json"), encoding="utf-8") as f:
            _JSON[kind] = json.load(f)
    return _JSON[kind]


def _package(kind):
    if PACKAGE not in sys.modules:
        top = types.ModuleType(PACKAGE)
        top.__path__ = []
        sys.modules[PACKAGE] = top
    full = f"{PACKAGE}.{kind}"
    mod = sys.modules.get(full)
    if mod is None:
        mod = types.ModuleType(full)
        mod.__path__ = [folder(kind)]
        mod.__package__ = full
        sys.modules[full] = mod
    return mod


def module(kind):
    """sinks/<kind>/sink.py of a registered kind (KeyError for one that isn't registered)."""
    folder(kind)
    full = f"{PACKAGE}.{kind}.sink"
    with _LOCK:
        mod = sys.modules.get(full)
        if mod is not None:
            return mod
        _package(kind)
        spec = importlib.util.spec_from_file_location(full, os.path.join(folder(kind), "sink.py"))
        if spec is None:
            raise ImportError(f"sinks/{kind}/sink.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(full, None)
            raise
        return mod


def hooks(kind):
    """sinks/<kind>/sink.py when kind is a registered kind, else None: events.json can be edited by hand, so a sink's
    "kind" may be anything (another word, a number, a list), and lib/events.py treats each such one as it always has."""
    return module(kind) if isinstance(kind, str) and kind in SINKS else None
