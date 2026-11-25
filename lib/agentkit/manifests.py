"""What shared code needs from each agent, read from its manifest (coding-agents/<name>/manifest.json), so no shared program
names an agent: its words, its command, its install options and the parts of install.sh, metacomputer agents, learn
and bench it takes part in.

A manifest is a JSON object, checked by check():
    label        its name in messages ("Claude Code", "Hermes Agent")
    flag         install.sh's word for it: --FLAG, --no-FLAG, --only-FLAG; its key in agents.json (your choices) and in
                 installed.json's "incomplete"
    prog         its command on PATH
    short        the short name a handoff's messages use for it ("Claude needs you")
    json_key     its key in `metacomputer agents --json` (default: flag)
    docs         its page for people (docs/<name>.md)
    owners       who reviews its folder (.github/CODEOWNERS)
    tools        how it names metacomputer's tools, for messages
    install      {"options": install.sh options of its own besides --FLAG/--no-FLAG/--only-FLAG, "missing": "line"
                 (an install that finds it missing says so on a line of its own) or "listed" (with the others in one
                 "Not installed here" line), "steps": "generic" (lib/agent_setup.py's install and uninstall) or "hooks"
                 (its setup.sh's), "only_note": what --only-FLAG says last, "switch": what of it --switch moves to
                 this checkout besides the command, in words}
    sessions     {"source": "files" (transcript files: lib/transcripts.py) or "databases" (lib/session_dbs.py),
                  "key": learn's count of its sessions (report["sources"][KEY]), "kinds": the Transcript kinds its
                  reader makes}: present when `metacomputer learn` reads its sessions
    bench        {"switch": its --agent word}: present when bench/bench.py runs it
    release_file a channel file of its at the repository's root whose top-level "version" is the release's
                 (scripts/release.sh sets it; at most one agent has one)
Only lib/agentkit, lib/agent_setup.py and scripts/gen-agents.py read it; bash reads it through lib/agentkit/agents.sh
(generated). A manifest is a contributor's input (a pull request may touch its folder only), and its values reach
generated shell, CODEOWNERS and docs: check() holds each to a whitelist of its own (lib/gensafe.py). Works under any
python3 (macOS's 3.9 included)."""
import re

from . import registry

KEYS = {"label", "flag", "prog", "short", "json_key", "docs", "owners", "tools", "install", "sessions", "bench",
        "release_file"}
REQUIRED = ("label", "flag", "prog", "short", "docs", "owners", "tools")
INSTALL_KEYS = {"options", "missing", "steps", "only_note", "switch"}
SESSION_KEYS = {"source", "key", "kinds"}
NAME = re.compile(r"[a-z][a-z0-9-]{0,40}")                 # an agent's name: its folder, its registry line
# Each field's whitelist: (pattern, what it is, its longest), or an int: one line of text at most that long.
FIELDS = {
    "label": 64, "short": 32, "tools": 120,
    "prog": (r"[A-Za-z0-9][A-Za-z0-9._+-]*", "a command's name (letters, digits and . _ + -)", 64),
    "json_key": (r"[a-z][a-z0-9_]*", "a JSON key (lowercase letters, digits and _)", 64),
    "release_file": (r"[A-Za-z0-9][A-Za-z0-9._-]*", "a file at the repository's root (letters, digits and . _ -)", 128),
}
OPTION = (r"--[a-z][a-z0-9-]+", "an option (--word: lowercase letters, digits and -)", 42)
WORD = (r"[a-z][a-z0-9_-]*", "a lowercase word (letters, digits, _ and -)", 41)


def manifest(name):
    """The agent's manifest with its defaults filled in."""
    raw = dict(registry.data(name, "manifest"))
    inst = dict(raw.get("install") or {})
    out = dict(raw)
    out["json_key"] = raw.get("json_key") or raw.get("flag")
    out["owners"] = tuple(raw.get("owners") or ())
    out["install"] = {"options": tuple(inst.get("options") or ()), "missing": inst.get("missing") or "listed",
                      "switch": inst.get("switch") or "",
                      "steps": inst.get("steps") or "generic",
                      "only_note": inst.get("only_note") or f"Start a new {raw.get('label')} session to pick this up."}
    return out


def label(name):
    return manifest(name)["label"]


def labels(names=None):
    return [label(n) for n in (registry.AGENTS if names is None else names)]


def in_words(words, last="or"):
    """"A, B, C or D" (last="" : "A, B, C, D")."""
    words = list(words)
    if not last or len(words) < 2:
        return ", ".join(words)
    return ", ".join(words[:-1]) + f" {last} " + words[-1]


def by_flag(flag):
    """The registered agent whose install.sh word is flag, else None."""
    return next((n for n in registry.AGENTS if manifest(n)["flag"] == flag), None)


def switch_words():
    """What --switch moves besides the command, in words: each agent's install "switch", joined with "and"."""
    return " and ".join(manifest(n)["install"]["switch"] for n in registry.AGENTS if manifest(n)["install"].get("switch"))


def release_file():
    """The channel file that carries the release's version (the one agent's "release_file"), or None."""
    files = [manifest(n)["release_file"] for n in registry.AGENTS if manifest(n).get("release_file")]
    return files[0] if files else None


def generic():
    """The agents whose install and uninstall are lib/agent_setup.py's (no setup.sh steps of their own)."""
    return [n for n in registry.AGENTS if manifest(n)["install"]["steps"] == "generic"]


def _field(v, rule):
    """None when v passes rule (FIELDS' form), else what it should be."""
    import gensafe              # lib/gensafe.py: only checks need it
    return gensafe.text(v, rule) if isinstance(rule, int) else gensafe.matches(v, rule[0], rule[1], rule[2])


def check(name):
    """What's wrong with an agent's manifest: [str] (empty: nothing). Each problem names its field."""
    import gensafe
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        return [f"the agent's name {name!r} isn't a lowercase word (a letter, then letters, digits and -)"]
    try:
        raw = registry.data(name, "manifest")
    except (OSError, ValueError) as e:
        return [f"coding-agents/{name}/manifest.json can't be read ({type(e).__name__})"]
    bad = []
    if not isinstance(raw, dict):
        return [f"coding-agents/{name}/manifest.json isn't an object"]
    bad += [f"unknown key {k!r}" for k in sorted(set(raw) - KEYS)]
    bad += [f"{k!r} is missing" for k in REQUIRED if not raw.get(k)]
    for k in ("label", "flag", "prog", "short", "json_key", "docs", "tools"):
        if k in raw and not isinstance(raw[k], str):
            bad.append(f"{k!r} isn't text")
    for k, rule in FIELDS.items():
        if isinstance(raw.get(k), str) and _field(raw[k], rule):
            bad.append(f"{k!r} isn't {_field(raw[k], rule)}")
    flag = raw.get("flag")
    if isinstance(flag, str) and not registry.PART.fullmatch(flag):
        bad.append("its flag isn't a plain lowercase word")
    if not isinstance(raw.get("owners"), list) or gensafe.owners(raw["owners"]):
        bad.append(f"'owners' isn't {gensafe.owners(None)}")
    inst = raw.get("install", {})
    if not isinstance(inst, dict) or set(inst) - INSTALL_KEYS:
        bad.append("'install' has keys this doesn't know")
    elif inst.get("missing", "listed") not in ("line", "listed") or inst.get("steps", "generic") not in ("generic", "hooks"):
        bad.append("'install' says something this doesn't know")
    else:
        opts = inst.get("options", [])
        if not isinstance(opts, list) or any(_field(o, OPTION) for o in opts):
            bad.append(f"install.options isn't a list, each {OPTION[1]}")
        for k in ("only_note", "switch"):
            if k in inst and gensafe.text(inst[k]):
                bad.append(f"install.{k} isn't {gensafe.text(inst[k])}")
    ses = raw.get("sessions")
    if ses is not None and (not isinstance(ses, dict) or set(ses) - SESSION_KEYS
                            or ses.get("source") not in ("files", "databases")):
        bad.append("'sessions' isn't one this knows")
    elif ses is not None and (_field(ses.get("key"), FIELDS["json_key"]) or not isinstance(ses.get("kinds", []), list)
                              or any(_field(x, WORD) for x in ses.get("kinds", []))):
        bad.append(f"sessions.key isn't {FIELDS['json_key'][1]}, or sessions.kinds isn't a list, each {WORD[1]}")
    bench = raw.get("bench")
    if bench is not None and (not isinstance(bench, dict) or set(bench) != {"switch"} or _field(bench["switch"], WORD)):
        bad.append(f"'bench' isn't {{\"switch\": {WORD[1]}}}")
    if "release_file" in raw and not (isinstance(raw["release_file"], str) and "/" not in raw["release_file"]):
        bad.append("its release_file isn't a file at the repository's root")
    if isinstance(raw.get("docs"), str) and not raw["docs"].startswith("docs/"):
        bad.append("its docs page isn't under docs/")
    elif isinstance(raw.get("docs"), str) and gensafe.docs_page(raw["docs"]):
        bad.append(f"'docs' isn't {gensafe.docs_page(None)}")
    return bad


def conflicts():
    """Words two agents both claim (a flag, a label, a JSON key): [str]."""
    out, seen = [], {}
    for n in registry.AGENTS:
        m = manifest(n)
        if m.get("release_file"):
            v = ("release_file", "*")       # one agent's at most
            if v in seen:
                out.append(f"{n} and {seen[v]} both have a release_file")
            seen[v] = n
        for k in ("flag", "label", "json_key", "prog"):
            v = (k, m.get(k))
            if v in seen:
                out.append(f"{n} and {seen[v]} both have {k} {m.get(k)!r}")
            seen[v] = n
    return out
