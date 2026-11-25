#!/usr/bin/env python3
"""How each agent's processes are recognised (coding-agents/<name>/recognition.json), as one set of tables: lib/owners.py reads
them (an agent session, for the one-controller rule, handoffs, an agent session's computer and the commands only the
user runs), and the vault's build embeds the same tables (scripts/build-vault.sh writes them as Swift with `swift`
below, so the vault has no second copy). The logic that reads them is lib/owners.py's and vault/Util.swift's, unchanged
by an agent's folder; tests/agent_argv_cases.py checks both against every agent's cases (coding-agents/<name>/tests/
recognition_cases.json).

A recognition.json is an object (check() says what's wrong with one):
    kinds            the agent sessions it is, by the names messages, claims, events and handoffs use ("Antigravity CLI",
                     "Antigravity app"); every other entry maps to one of these
    executables      {FILE NAME: KIND}: a process whose own executable (its name, or its executable's path) has this
                     file name ("claude"; never a script an interpreter runs)
    version_folders  {FOLDER: KIND}: a process whose executable is a version file in that folder (.../FOLDER/1.2.3)
    names            {FILE NAME: KIND}: a process's name, an interpreter's title (argv[0]) or the file name of the script
                     it runs ("gemini", "openclaw-gateway")
    apps             [{"kind", "bundle", "helper", "folders"}]: an app's executables by their path: one the regular
                     expression bundle finds in, or one whose file name helper matches in a folder of the app's own (a
                     path component that is or starts with one of folders, case aside: folders are written lowercase)
    path_words       {WORD: KIND}: a word in the path of the script an interpreter runs ("hermes_cli")
    modules          {MODULE: KIND}: python -m MODULE (its package)
    package_words    {IDENTIFIER: KIND}: an identifier specific to the agent's package, in any argument of an
                     interpreter or in what one naming a file resolves to ("@google/gemini-cli")
    package_folders  {FOLDER: {"kind", "helpers"}}: a folder of that name in such a path, except below it the
                     agent's own helpers' path (helpers: its next components), which aren't the session
    packages         {PACKAGE: KIND}: a package a preload names (node --require/--import PACKAGE)
    node_scripts     {PREFIX: KIND}: the vault only: node running a script (its first two arguments) whose file name
                     starts with PREFIX
    shared           {KIND: {"label", "under", "always", "titles", "subcommands", "within"}}: the vault only: a process
                     of that kind that runs all of its conversations (no single session): always, or with no readable
                     arguments, a title (argv[0]'s file name) in titles, or one of subcommands among its arguments after
                     the first (within: only the first N of those); label names it in the vault's records, and the
                     question says "under UNDER, whose conversations share one process"
When two agents' words both match (a nonsensical process), the longest word is tried first (the most specific), then
the registry's order. A record without an agent field (a claim, a handoff, the vault's records) is the registry's DEFAULT
agent's (record_default: its label), for lib/owners.py and the vault alike.

An app's bundle and helper are searched by Python's re (lib/owners.py) and by the vault's Foundation/ICU
(vault/Util.swift matches), so they keep to what both compile and read alike (unportable() says what's outside it):
printable ASCII characters other than \\ . ^ $ | ? * + ( ) [ ] { } as themselves; one of those, / or - escaped (\\.
\\/); \\d and \\D; a class [...] or [^...] of such characters, escapes and ranges (a-z), with \\d or \\D, never [
inside it, && || ~~, an unescaped - that isn't a range, or ] first; * + ? {m} {m,} {m,n} (n at most 1000) after one
of those or a group, never after another; (...) and (?:...); | ; ^ and $. Everything else is refused: the dot (ICU's
doesn't match \\r, U+0085, U+2028 or U+2029), \\w and \\s (they differ outside ASCII and on U+001C to U+001F), other
escapes, every other (?...) (named groups in either syntax, (?P=...), (?#...), lookarounds, inline flags), lazy or
possessive quantifiers, a { that isn't one, characters outside printable ASCII (ICU repeats a letter with its
combining marks as one), and the empty pattern (Python's matches everything, ICU's doesn't compile). What still
differs: $ before a line break that ends the text (Python's matches before a final \\n, ICU's before U+0085 and U+2028
too, and the vault's call not always before \\n), which an executable's path never ends with.

    python3 lib/agentkit/recognition.py swift     the tables as Swift (scripts/build-vault.sh)
    python3 lib/agentkit/recognition.py check     exit 1 and say what's wrong with the agents' recognition.json files
Works under any python3 (macOS's 3.9 included)."""
import os
import re
import sys

if __package__ in (None, ""):                      # run as a program: the framework as a package
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agentkit import manifests, registry      # noqa: E402
else:
    from . import manifests, registry

KEYS = {"kinds", "executables", "version_folders", "names", "apps", "path_words", "modules", "package_words",
        "package_folders", "packages", "node_scripts", "shared"}
WORD_TABLES = ("executables", "version_folders", "names", "path_words", "modules", "package_words", "packages",
               "node_scripts")
SHARED_KEYS = {"label", "under", "always", "titles", "subcommands", "within"}


def raw(name):
    return registry.data(name, "recognition")


def _longest_first(pairs):
    """[(word, ...)] with the longest word first; equal lengths keep their order (the registry's, then the file's)."""
    return tuple(sorted(pairs, key=lambda p: -len(p[0])))


def tables():
    """Every agent's recognition, merged in the registry's order."""
    t = {"kinds": [], "executables": {}, "version_folders": [], "names": {}, "apps": [], "path_words": [],
         "modules": {}, "package_words": [], "package_folders": [], "packages": {}, "node_scripts": [], "shared": {}}
    for n in registry.AGENTS:
        r = raw(n)
        t["kinds"] += list(r.get("kinds") or ())
        for k in ("executables", "names", "modules", "packages"):
            t[k].update(r.get(k) or {})
        t["version_folders"] += list((r.get("version_folders") or {}).items())
        t["path_words"] += list((r.get("path_words") or {}).items())
        t["package_words"] += list((r.get("package_words") or {}).items())
        t["node_scripts"] += list((r.get("node_scripts") or {}).items())
        t["package_folders"] += [(f, v["kind"], tuple(v.get("helpers") or ())) for f, v in
                                 (r.get("package_folders") or {}).items()]
        t["apps"] += [dict(a) for a in r.get("apps") or ()]
        t["shared"].update(r.get("shared") or {})
    for k in ("path_words", "package_words", "package_folders"):
        t[k] = _longest_first(t[k])
    t["kinds"] = tuple(t["kinds"])
    return t


def record_default():
    """The kind a record without an "agent" field names (a claim, a handoff, the vault's records): the registry's
    DEFAULT agent's label, one of its kinds. lib/owners.py reads it, and swift() writes it for the vault."""
    return manifests.label(registry.DEFAULT)


# What both engines read as one character when it's escaped (outside a class and in one), and the escapes that differ.
_ESCAPED = frozenset("\\.^$|?*+()[]{}/-")
_ESCAPE_WHY = {"w": "Python's and ICU's differ outside ASCII (combining marks, superscripts): write [A-Za-z0-9_]",
               "s": "Python's also matches U+001C to U+001F: write the characters",
               "b": "a word boundary, by \\w, which differs",
               "Z": "Python's is the end, ICU's also before a final line break",
               "v": "Python's is U+000B, ICU's any vertical space"}
_OUTSIDE_ASCII = ("a character outside printable ASCII (ICU repeats a letter with its combining marks as one: write a "
                  "class such as [^/] for the rest)")
MAX_REPEAT = 1000
_REPEAT = re.compile(r"\{([0-9]+)(?:(,)([0-9]*))?\}")
_GROUPS = (("P<", "a named group (?P<name>...)"), ("P=", "a named backreference (?P=name)"),
           ("P>", "a group call (?P>name)"), ("#", "a comment (?#...)"), ("<=", "a lookbehind (?<=...)"),
           ("<!", "a lookbehind (?<!...)"), ("=", "a lookahead (?=...)"), ("!", "a lookahead (?!...)"),
           ("<", "a named group (?<name>...)"), ("'", "a named group (?'name'...)"), (">", "an atomic group (?>...)"),
           ("(", "a conditional (?(...)...)"), ("|", "a branch reset (?|...)"))


def _escape(e):
    """The words for an escape outside the shared set (\\e)."""
    why = "a backreference" if e.isdigit() else _ESCAPE_WHY.get(e) or _ESCAPE_WHY.get(e.lower())
    return f"\\{e}" + (f" ({why})" if why else ", an escape outside the shared set")


def _group(rest):
    """The words for a group (?REST... that isn't (?:...)."""
    for start, words in _GROUPS:
        if rest.startswith(start):
            return words
    flags = re.match(r"[-A-Za-z]+", rest)
    return f"inline flags (?{flags.group(0)}...)" if flags else f"the group (?{rest[:1]}"


def _class(p, i):
    """(the index after the class that starts at p[i], None), or (None, what in it is outside the shared set)."""
    j, n = i + 1, len(p)
    if j < n and p[j] == "^":
        j += 1
    if j < n and p[j] == "]":
        return None, "a class whose first character is ] (write \\])"
    if j < n and p[j] == ":":
        return None, "a class whose first character is : (ICU reads [:name:] as a POSIX class: write \\:)"
    while j < n:
        c = p[j]
        if c == "]":
            return j + 1, None
        if c == "[":
            return None, "[ inside a class (ICU reads a nested set: write \\[)"
        if c in "&|~" and p.startswith(c * 2, j):
            return None, f"{c * 2} inside a class (read as a set operation)"
        if c == "-":
            return None, "an unescaped - inside a class that isn't a range (write \\-)"
        if c == "\\":
            e = p[j + 1:j + 2]
            if e in ("d", "D"):
                j += 2
                continue                    # a class of its own: no range starts from it
            if e not in _ESCAPED:
                return (None, "a trailing \\") if not e else (None, _escape(e))
            lo, j = e, j + 2
        elif not " " <= c <= "~":
            return None, _OUTSIDE_ASCII
        else:
            lo, j = c, j + 1
        if p.startswith("-", j) and j + 1 < n and p[j + 1] != "]":     # a range lo-hi
            k = j + 1
            if p[k] == "\\" and p[k + 1:k + 2] in _ESCAPED and p[k + 1:k + 2]:
                hi, k = p[k + 1], k + 2
            elif p[k] in "\\[-":
                return None, "a range that doesn't end in one character"
            elif not " " <= p[k] <= "~":
                return None, _OUTSIDE_ASCII
            else:
                hi, k = p[k], k + 1
            if hi < lo:
                return None, f"the range {lo}-{hi}, from a later character to an earlier one"
            j = k
    return None, "an unterminated class ["


def unportable(p):
    """What in the regular expression p (an app's bundle or helper) is outside what Python's re and the vault's ICU both
    compile and read alike (the module's docstring has the list), in words; None when nothing is."""
    if not isinstance(p, str):
        return "a pattern that isn't a string"
    if not p:
        return "the empty pattern (Python's matches everything; ICU's doesn't compile)"
    i, n, depth, atom = 0, len(p), 0, False     # atom: what came last may be repeated
    while i < n:
        c = p[i]
        if c == "\\":
            e = p[i + 1:i + 2]
            if not e:
                return "a trailing \\"
            if e not in _ESCAPED and e not in ("d", "D"):
                return _escape(e)
            i, atom = i + 2, True
        elif c == "[":
            i, why = _class(p, i)
            if why:
                return why
            atom = True
        elif c == "(":
            if p.startswith("(?", i) and not p.startswith("(?:", i):
                return _group(p[i + 2:])
            i, depth, atom = i + (3 if p.startswith("(?:", i) else 1), depth + 1, False
        elif c == ")":
            if not depth:
                return "an unbalanced )"
            i, depth, atom = i + 1, depth - 1, True
        elif c in "*+?{":
            m = _REPEAT.match(p, i) if c == "{" else None
            if c == "{" and (not m or (m.group(3) and int(m.group(3)) < int(m.group(1)))
                             or max(int(m.group(1)), int(m.group(3) or 0)) > MAX_REPEAT):
                return (f"a {{ that isn't a repeat {{m}}, {{m,}} or {{m,n}} with m <= n <= {MAX_REPEAT} (write \\{{ "
                        f"for the character)")
            q = p[i:m.end()] if m else c
            if not atom:
                return f"{q} with nothing before it to repeat"
            i, atom = i + len(q), False
            if i < n and p[i] in "*+?{":
                return f"a repeat after a repeat ({q}{p[i]}: lazy, possessive or doubled)"
        elif c == ".":
            return "an unescaped . (ICU's doesn't match \\r, U+0085, U+2028 or U+2029: write [^/] or \\.)"
        elif c in "]}":
            return f"an unescaped {c} (write \\{c})"
        elif not " " <= c <= "~":
            return _OUTSIDE_ASCII
        else:                                       # | ^ $ and the characters that are themselves
            i, atom = i + 1, c not in "|^$"
    return "an unbalanced (" if depth else None


def check():
    """What's wrong with the agents' recognition.json files: [str] (empty: nothing)."""
    bad, kinds, words = [], {}, {}
    for n in registry.AGENTS:
        try:
            r = raw(n)
        except (OSError, ValueError) as e:
            bad.append(f"coding-agents/{n}/recognition.json can't be read ({type(e).__name__})")
            continue
        if not isinstance(r, dict):
            bad.append(f"coding-agents/{n}/recognition.json isn't an object")
            continue
        bad += [f"{n}: unknown key {k!r}" for k in sorted(set(r) - KEYS)]
        own = r.get("kinds") or []
        if not own or not all(isinstance(k, str) and k for k in own):
            bad.append(f"{n}: no kinds")
        for k in own:
            if k in kinds:
                bad.append(f"{n}: the kind {k!r} is {kinds[k]}'s too")
            kinds[k] = n
        if manifests.manifest(n)["label"] not in own:
            bad.append(f"{n}: its manifest's label isn't one of its kinds")
        for table in WORD_TABLES:
            for w, kind in (r.get(table) or {}).items():
                if not isinstance(w, str) or not w or kind not in own:
                    bad.append(f"{n}: {table} {w!r} names a kind that isn't its own ({kind!r})")
                if (table, w) in words:
                    bad.append(f"{n}: {table} {w!r} is {words[(table, w)]}'s too")
                words[(table, w)] = n
        for f, v in (r.get("package_folders") or {}).items():
            if not isinstance(v, dict) or v.get("kind") not in own or not all(isinstance(h, str) for h in v.get("helpers") or ()):
                bad.append(f"{n}: package_folders {f!r} isn't {{kind, helpers}} of its own")
        for i, a in enumerate(r.get("apps") or ()):
            odd = {f: unportable(a[f]) for f in ("bundle", "helper")
                   if isinstance(a, dict) and isinstance(a.get(f), str)}
            odd = {f: what for f, what in odd.items() if what}
            bad += [f"{n}: apps[{i}].{f} uses {what}, which the vault's regular expressions (ICU) don't read as "
                    f"Python's do (lib/agentkit/recognition.py says what both read alike)" for f, what in odd.items()]
            try:
                ok = a["kind"] in own and all(f in odd or re.compile(a[f]) for f in ("bundle", "helper")) and \
                    all(isinstance(x, str) and x for x in a["folders"])
            except (KeyError, TypeError, re.error):
                ok = False
            if not ok:
                bad.append(f"{n}: an app isn't {{kind, bundle, helper, folders}} of its own with regular expressions")
            elif not all(x == x.lower() for x in a["folders"]):
                bad.append(f"{n}: an app's folders aren't lowercase (each folder of a path is lowercased before it's "
                           f"compared with them)")
        for kind, s in (r.get("shared") or {}).items():
            if kind not in own or not isinstance(s, dict) or set(s) - SHARED_KEYS or not s.get("label") \
                    or not s.get("under") or not isinstance(s.get("within", None), (int, type(None))):
                bad.append(f"{n}: shared {kind!r} isn't one this knows")
    return bad


# --- the vault's copy, as Swift ----------------------------------------------------------------------------------
def _s(v):
    """A Swift string literal."""
    out = []
    for c in v:
        if c in '\\"':
            out.append("\\" + c)
        elif ord(c) < 0x20 or ord(c) == 0x7f:
            out.append("\\u{%x}" % ord(c))
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _dict(d):
    return "[" + ", ".join(f"{_s(k)}: {_s(v)}" for k, v in d.items()) + "]" if d else "[:]"


def _pairs(ps):
    return "[" + ", ".join(f"({_s(a)}, {_s(b)})" for a, b in ps) + "]"


def _list(xs):
    return "[" + ", ".join(_s(x) for x in xs) + "]"


def swift():
    """The tables as Swift source: vault/Util.swift's agentKind and the Requester read them (scripts/build-vault.sh
    compiles this file with the vault's own)."""
    t = tables()
    labels = manifests.labels()
    shared = []
    for kind, s in t["shared"].items():
        within = "nil" if s.get("within") is None else str(int(s["within"]))
        shared.append(f"    {_s(kind)}: SharedAgentKind(label: {_s(s['label'])}, under: {_s(s['under'])}, "
                      f"always: {'true' if s.get('always') else 'false'}, titles: {_list(s.get('titles') or ())}, "
                      f"subcommands: {_list(s.get('subcommands') or ())}, within: {within})")
    apps = [f"    AgentApp(kind: {_s(a['kind'])}, bundle: {_s(a['bundle'])}, helper: {_s(a['helper'])}, "
            f"folders: {_list(a['folders'])})" for a in t["apps"]]
    folders = [f"({_s(f)}, {_s(k)}, {_list(h)})" for f, k, h in t["package_folders"]]
    lines = [
        "// Generated by scripts/build-vault.sh from the agents' recognition.json files (lib/agentkit/recognition.py",
        "// swift): the tables vault/Util.swift's agentKind and Requester read. Not kept in the repository.",
        f"let agentExecutables: [String: String] = {_dict(t['executables'])}",
        f"let agentVersionFolders: [(String, String)] = {_pairs(t['version_folders'])}",
        f"let agentNodeScripts: [(String, String)] = {_pairs(t['node_scripts'])}",
        f"let agentNames: [String: String] = {_dict(t['names'])}",
        "let agentApps: [AgentApp] = [" + ("\n" + ",\n".join(apps) + "]" if apps else "]"),
        f"let agentPathWords: [(String, String)] = {_pairs(t['path_words'])}",
        f"let agentModules: [String: String] = {_dict(t['modules'])}",
        f"let agentPackageWords: [(String, String)] = {_pairs(t['package_words'])}",
        "let agentPackageFolders: [(String, String, [String])] = [" + ", ".join(folders) + "]",
        f"let agentPackages: [String: String] = {_dict(t['packages'])}",
        "let sharedAgentKinds: [String: SharedAgentKind] = [" + ("\n" + ",\n".join(shared) + "]" if shared else ":]"),
        f"let agentSessionLabels = {_s(manifests.in_words(labels))}",
        f"let recordDefaultAgent = {_s(record_default())}",
    ]
    return "\n".join(lines) + "\n"


def main(argv):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    what = strictargs.expect(argv, ["swift|check"], prog="recognition.py", usage=__doc__).words[0]
    bad = check()
    if what == "check":
        for b in bad:
            print(b)
        return 1 if bad else 0
    if bad:
        print("recognition.py: " + "; ".join(bad), file=sys.stderr)
        return 1
    sys.stdout.write(swift())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
