"""The providers metacomputer knows: an explicit list, so adding one is its folder plus one reviewed line here.

Each name is a folder providers/<name>/ at the top of the repository (providers/README.md says what one holds): its
adapter (adapter.py, whose ADAPTER is the class lib/cloud registers), its manifest (manifest.py: capabilities, and the
words, options, settings and credentials the command line needs), and whatever else it brings. Their order here is the
order they're listed in (metacomputer list, the docs' index, messages that name every provider).

A provider's modules are loaded from its folder under the private package name `mcomp_providers` (so a provider's
code imports its own files relatively and the framework as `cloud`), never by putting the repository on sys.path,
where another installed package of the same name could stand in for them."""
import importlib
import importlib.util
import os
import re
import sys

PROVIDERS = (
    "oci",
    "sprites",
    "hetzner",
    "digitalocean",
    "vultr",
)

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "providers")
PACKAGE = "mcomp_providers"
PART = re.compile(r"[a-z][a-z0-9_]{0,40}")


def _package():
    mod = sys.modules.get(PACKAGE)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(PACKAGE, os.path.join(ROOT, "__init__.py"),
                                                  submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(PACKAGE, None)
        raise
    return mod


def folder(name):
    """providers/<name>, for a registered provider; KeyError otherwise."""
    if name not in PROVIDERS:
        raise KeyError(name)
    return os.path.join(ROOT, name)


def has(name, part):
    """Whether the registered provider `name` brings providers/<name>/<part>.py (a module of hooks, say)."""
    return bool(PART.fullmatch(part)) and os.path.isfile(os.path.join(folder(name), part + ".py"))


def module(name, part="adapter"):
    """The module providers/<name>/<part>.py of a registered provider (KeyError for one that isn't registered)."""
    folder(name)
    if not PART.fullmatch(part):
        raise ValueError(f"not a module's name: {part!r}")
    _package()
    return importlib.import_module(f"{PACKAGE}.{name}.{part}")


def adapter_class(name):
    """The adapter class providers/<name>/adapter.py registers (its ADAPTER)."""
    return module(name).ADAPTER


def manifest(name):
    """providers/<name>/manifest.py: its capability manifest (MANIFEST) and command-line data (CLI)."""
    return module(name, "manifest")


def hooks(name, part="remote"):
    """providers/<name>/<part>.py when the provider brings it (its hooks into a shared program), else None."""
    return module(name, part) if name in PROVIDERS and has(name, part) else None
