"""The coding agents metacomputer is set up for: an explicit list, so adding one is its folder plus one reviewed line
here.

Each name is a folder coding-agents/<name>/ at the top of the repository (coding-agents/README.md says what one holds): its manifest
(manifest.json: its words, its command, its install options, its docs; lib/agentkit/manifests.py), how its processes
are recognised (recognition.json: lib/agentkit/recognition.py, which the vault's build embeds too), and the hooks it
brings (setup.py, setup.sh, cli.sh, sessions.py, bench.py, channel.py: each optional). Their order here is the order
they're listed and set up in (metacomputer agents, install.sh's steps, messages that name every agent, docs/agents.md).

An agent's modules are loaded from its folder under the private package name `mcomp_agents` (the folder's name with
"-" as "_": mcomp_agents.claude_code), never by putting the repository on sys.path, where another installed package of
the same name could stand in for them. Works under any python3 (macOS's 3.9 included)."""
import importlib.util
import json
import os
import re
import sys
import threading
import types

AGENTS = (
    "claude-code",
    "codex",
    "openclaw",
    "hermes",
    "gemini",
    "antigravity",
)

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "coding-agents")
PACKAGE = "mcomp_agents"
PART = re.compile(r"[a-z][a-z0-9_]{0,40}")
_JSON = {}
_LOCK = threading.RLock()           # one thread loads a module; another waits for it, never sees it half-made


def folder(name):
    """coding-agents/<name>, for a registered agent; KeyError otherwise."""
    if name not in AGENTS:
        raise KeyError(name)
    return os.path.join(ROOT, name)


def ident(name):
    """The agent's name as a Python and shell identifier: claude-code -> claude_code."""
    return name.replace("-", "_")


def has(name, part):
    """Whether the registered agent `name` brings coding-agents/<name>/<part> (a file: "setup.py", "setup.sh", ...)."""
    return os.path.isfile(os.path.join(folder(name), part))


def data(name, part):
    """coding-agents/<name>/<part>.json, read once (manifest, recognition)."""
    key = (name, part)
    if key not in _JSON:
        with open(os.path.join(folder(name), part + ".json"), encoding="utf-8") as f:
            _JSON[key] = json.load(f)
    return _JSON[key]


def _package(name):
    if PACKAGE not in sys.modules:
        top = types.ModuleType(PACKAGE)
        top.__path__ = []
        sys.modules[PACKAGE] = top
    full = f"{PACKAGE}.{ident(name)}"
    mod = sys.modules.get(full)
    if mod is not None:
        return mod
    mod = types.ModuleType(full)
    mod.__path__ = [folder(name)]
    mod.__package__ = full
    sys.modules[full] = mod
    return mod


def module(name, part):
    """The module coding-agents/<name>/<part>.py of a registered agent (KeyError for one that isn't registered)."""
    folder(name)
    if not PART.fullmatch(part):
        raise ValueError(f"not a module's name: {part!r}")
    full = f"{PACKAGE}.{ident(name)}.{part}"
    with _LOCK:
        _package(name)
        mod = sys.modules.get(full)
        if mod is not None:
            return mod
        spec = importlib.util.spec_from_file_location(full, os.path.join(folder(name), part + ".py"))
        if spec is None:
            raise ImportError(f"coding-agents/{name}/{part}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(full, None)
            raise
        return mod


def hooks(name, part):
    """coding-agents/<name>/<part>.py when the agent brings it (its hooks into a shared program), else None."""
    return module(name, part) if name in AGENTS and has(name, part + ".py") else None


def each(part):
    """[(name, module)] of every registered agent that brings coding-agents/<name>/<part>.py, in the registry's order."""
    return [(n, module(n, part)) for n in AGENTS if has(n, part + ".py")]
