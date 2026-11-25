"""Hostile manifests: what a contributor's manifest could put in a file made from it, tried for every destination.

The kinds of sink, the providers and the coding agents take pull requests that touch one folder (sinks/, providers/,
coding-agents/), so a manifest's values are a contributor's input to generated shell (bin/metacomputer's help line,
lib/cloud/providers.sh, lib/agentkit/agents.sh), to the vault's build (Swift: lib/sinkkit/vault.py,
lib/agentkit/recognition.py), to a URL or a header, to CODEOWNERS and to the docs. Each value is checked against a
whitelist of its own before anything is made (lib/gensafe.py and each framework's check), and written for where it
lands. Every case below is tried twice: the check refuses it, naming the folder and the field (and so do the generator
and scripts/build-vault.sh, which make nothing); and the generator's writer, handed the value without the check, still
writes it inert (a shell word, a comment, a Swift literal, Markdown text or code, or a refusal for CODEOWNERS).

    sinks(check)       tests/test_events.py
    providers(check)   tests/test_cloud_adapters.py
    agents(check)      tests/test_agents.py
check is the suite's own (name, condition, detail). Nothing here touches the network; the shell it runs is the
generated text, in an empty temporary folder, and a hostile value's commands only ever create a marker file there."""
import contextlib
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
MARK = "hostile-ran"                    # the file a hostile value's command makes, if it ever runs


# --- helpers -----------------------------------------------------------------------------------------------------
def script(name):
    """scripts/<name>.py, loaded as a module of its own."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_hostile",
                                                  os.path.join(LAB, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def outcome(fn, *a):
    """("ok", result) or ("refused", str(exception)) for any Exception."""
    try:
        return "ok", fn(*a)
    except Exception as e:           # noqa: BLE001  (what each writer raises is the point)
        return "refused", f"{type(e).__name__}: {e}"


def named(problems, *words):
    """Whether one problem (a str, or one of a list) names every one of words."""
    items = [problems] if isinstance(problems, str) else list(problems or ())
    return any(all(w in p for w in words) for p in items)


def swift_literal(text, name):
    """The Swift literal `let NAME ... = "..."` holds in text (the whole value after =), or None."""
    m = re.search(rf"^let {re.escape(name)}(?:: [^=]+)? = (.*)$", text, re.M)
    return m.group(1) if m else None


def swift_prefix(text):
    """(the value of the Swift string literal text starts with, what follows it), or (None, text) when it doesn't start
    with one: a raw line break or control character in it, an escape Swift doesn't have, or no closing quote."""
    if not text or text[0] != '"':
        return None, text
    out, i = [], 1
    while i < len(text):
        c = text[i]
        if c == '"':
            return "".join(out), text[i + 1:]
        if c == "\\":
            m = re.match(r'\\(?:([\\"])|u\{([0-9a-fA-F]{1,8})\})', text[i:])
            if not m:
                return None, text
            out.append(m.group(1) or chr(int(m.group(2), 16)))
            i += m.end()
            continue
        if ord(c) < 0x20 or ord(c) == 0x7f:
            return None, text
        out.append(c)
        i += 1
    return None, text


def swift_decode(literal):
    """The value of a Swift string literal that's all of `literal` (nothing after its closing quote), or None."""
    value, rest = swift_prefix(literal)
    return value if value is not None and rest == "" else None


def quiet(fn, *a):
    """outcome(fn, *a), with what fn prints kept out of the suite's log."""
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return outcome(fn, *a)


def sh_run(text, calls=(":",), name="gen.sh"):
    """Writes text to NAME in an empty temporary folder and runs bash there: `. ./NAME` then each of calls, one bash
    each. ([each call's (status, stdout)], whether a hostile value's command ran: the marker file is there)."""
    t = tempfile.mkdtemp(prefix="mcomp-hostile-")
    try:
        with open(os.path.join(t, name), "w") as f:
            f.write(text)
        out = []
        for c in calls:
            r = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-c", f". ./{name}; {c}"], cwd=t,
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                               env={"PATH": "/usr/bin:/bin", "HOME": t})
            out.append((r.returncode, r.stdout))
        return out, any(f.startswith(MARK) for f in os.listdir(t))
    finally:
        shutil.rmtree(t, ignore_errors=True)


CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def md_problems(text, original):
    """What a hostile value did to a generated Markdown file: a line it added, a table row whose cells it changed, raw
    HTML or a link it brought (outside code spans, where they'd be read)."""
    bad = []
    if len(text.split("\n")) != len(original.split("\n")):
        bad.append(f"lines: {len(original.split(chr(10)))} -> {len(text.split(chr(10)))}")
    cells = None
    for ln in text.split("\n"):
        if not ln.startswith("|"):
            cells = None
            continue
        n = len(re.split(r"(?<!\\)\|", ln))
        cells = cells or n
        if n != cells:
            bad.append(f"a row of {n - 2} cells, not {cells - 2}: {ln[:100]}")
    for pattern, what in ((r"(?<!\\)<(?:img|script)", "raw HTML"), (r"(?<!\\)\]\((?:javascript|https?):", "a link")):
        seen = [len(re.findall(pattern, CODE_SPAN.sub("code", t))) for t in (text, original)]
        if seen[0] > seen[1]:
            bad.append(what)
    return bad


def codeowners_problems(outcome_, path):
    """A hostile owner either stops the block ("refused"), or it adds no line but its folder's (one line each)."""
    kind, block = outcome_
    if kind == "refused":
        return []
    lines = ("\n".join(block) if isinstance(block, list) else str(block)).split("\n")
    return [ln for ln in lines if ln and not ln.startswith(("#", "/"))] + \
        [ln for ln in lines if ln.startswith(path) and len(ln.split()) < 2]


@contextlib.contextmanager
def build_root(edit):
    """A copy of what scripts/build-vault.sh reads, edit(root) applied to it, and stand-ins for swiftc (it leaves a
    marker: the build got past every check) and codesign. Yields (run(), the marker's path)."""
    t = tempfile.mkdtemp(prefix="mcomp-hostile-build-")
    try:
        root = os.path.join(t, "lab")
        for d in ("lib", "sinks", "coding-agents", "vault", "config"):
            shutil.copytree(os.path.join(LAB, d), os.path.join(root, d), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(root, "scripts"))
        for f in ("build-vault.sh", "strictargs.sh", "safe-rmtree.sh", "lib.sh"):    # lib.sh: safe-rmtree's own checks
            shutil.copy2(os.path.join(LAB, "scripts", f), os.path.join(root, "scripts", f))
        edit(root)
        stubs = os.path.join(t, "stubs")
        os.makedirs(stubs)
        marker = os.path.join(t, "swiftc-ran")
        for prog, body in (("swiftc", f": > '{marker}'\nexit 1\n"), ("codesign", "exit 1\n")):
            with open(os.path.join(stubs, prog), "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(os.path.join(stubs, prog), 0o755)

        def run():
            return subprocess.run(["/bin/bash", os.path.join(root, "scripts", "build-vault.sh"), "--test",
                                   os.path.join(t, "out", "mcomp-vault")], capture_output=True, text=True, timeout=120,
                                  stdin=subprocess.DEVNULL, env={"PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
                                                                 "HOME": os.path.join(t, "home")})
        yield run, marker
    finally:
        shutil.rmtree(t, ignore_errors=True)


def edit_json(root, rel, change):
    p = os.path.join(root, rel)
    with open(p) as f:
        d = json.load(f)
    change(d)
    with open(p, "w") as f:
        json.dump(d, f, indent=2)


def build_refuses(check, what, rel, change, words):
    """scripts/build-vault.sh, with change made to rel's JSON in a copy: it stops before swiftc, saying words; the
    same copy without the change gets to swiftc (so it was the check that stopped it)."""
    with build_root(lambda root: None) as (run, marker):
        run()
        clean = os.path.exists(marker)
    with build_root(lambda root: edit_json(root, rel, change)) as (run, marker):
        r = run()
        reached = os.path.exists(marker)
    check(f"scripts/build-vault.sh refuses {what} before it compiles anything, naming the folder and the field (the "
          "same copy without it gets to swiftc)", clean and r.returncode != 0 and not reached
          and all(w in r.stderr for w in words), (clean, r.returncode, reached, r.stderr[-400:]))


# --- the kinds of sink (lib/sinkkit; scripts/gen-sinks.py, lib/sinkkit/vault.py) -------------------------------------
def sinks(check):
    print("hostile sink manifests (every value that reaches generated shell, Swift, a URL, a header, CODEOWNERS, docs)")
    from sinkkit import manifests, registry, vault
    gen = script("gen-sinks")
    real = registry.data

    @contextlib.contextmanager
    def patched(kind, change):
        raw = copy.deepcopy(real(kind))
        change(raw)
        registry.data = lambda k: raw if k == kind else real(k)
        try:
            yield
        finally:
            registry.data = real

    def fmt(key, value):
        return lambda raw: raw["format"].__setitem__(key, value)

    def top(key, value):
        return lambda raw: raw.__setitem__(key, value)

    def refused(kind, change, field):
        """(vault.check's problems, gen-sinks' outcome) for kind's manifest with change; the first names the folder
        and the field when it refuses."""
        with patched(kind, change):
            bad = vault.check()
            made = outcome(gen.wanted)
        return bad, made, named(bad, f"sinks/{kind}/manifest.json", field) and made[0] == "refused" \
            and f"sinks/{kind}/manifest.json" in made[1] and field in made[1]

    # P1: a path that changes the URL's authority (Codex's case: https://collector.example@attacker.example/v1/logs)
    hostile_paths = ["@attacker.example/v1/logs", "//attacker.example/v1/logs", "/v1/logs/../../x", "v1/logs",
                     "/v1/logs?x=1", "/v1/logs#f", "/v1/l%40gs", "/v1/logs\n", ":8443/v1/logs", "/v1/logs@x"]
    got = {p: refused("otlp", fmt("path", p), "format.path")[2] for p in hostile_paths}
    check("an OTLP format.path that could change the URL's host, port, query or fragment (@attacker.example/v1/logs, "
          "//host, .., no leading /, ?, #, %, a line break, :port, @) is refused by the check and by gen-sinks, "
          "naming sinks/otlp/manifest.json and format.path", all(got.values()), {p: v for p, v in got.items() if not v})
    with patched("otlp", fmt("path", "@attacker.example/v1/logs")):
        swift_main = quiet(vault.main, ["swift"])
    check("...and lib/sinkkit/vault.py swift (what build-vault.sh compiles in) writes nothing for it (exit 1)",
          swift_main == ("ok", 1), swift_main)
    check("...while the checked-in /v1/logs passes", not refused("otlp", fmt("path", "/v1/logs"), "format.path")[0])
    build_refuses(check, "an OTLP format.path of @attacker.example/v1/logs", "sinks/otlp/manifest.json",
                  lambda d: d["format"].__setitem__("path", "@attacker.example/v1/logs"),
                  ("sinks/otlp/manifest.json", "format.path"))

    # P1: help text that ends bin/metacomputer's comment line and runs a command
    hostile_help = "webhook\n: > " + MARK + "\n#"
    for h in (hostile_help, "web\rhook", "web\x1b[2Jhook", "web hook", "web‮kooh", "x" * 65):
        bad, made, ok = refused("webhook", top("help", h), "'help'")
        check(f"help {h[:24]!r} is refused by the check and by gen-sinks, naming the kind and 'help'", ok, (bad, made))
    orig = gen.read("bin/metacomputer")
    for h, why in ((hostile_help, "a line break"),
                   ("x $(: > " + MARK + "1) `: > " + MARK + "2`; : > " + MARK + "3 & | <x >y", "shell metacharacters")):
        with patched("webhook", top("help", h)):
            new = outcome(gen.usage_line, orig)
        if new[0] != "ok":
            check(f"gen-sinks writes a help with {why} into a comment, inert", False, new)
            continue
        text = new[1]
        code_lines = (lambda s: [ln for ln in s.split("\n") if not ln.lstrip().startswith("#")])
        at = next(i for i, ln in enumerate(text.split("\n")) if re.match(r"#\s+the optional event stream: ", ln))
        header = "\n".join(text.split("\n")[at:at + 1 + len(text.split("\n")) - len(orig.split("\n"))])
        _out, ran = sh_run(header + "\n")
        check(f"...and gen-sinks' writer, handed help with {why} past the check, keeps it inside the comment: "
              "bin/metacomputer gets no line and no command, and its line runs nothing",
              len(text.split("\n")) == len(orig.split("\n")) and code_lines(text) == code_lines(orig) and not ran,
              (len(orig.split("\n")), len(text.split("\n")), ran))

    # P2: a port the vault's UInt16 can't hold, and the other numbers out of their ranges
    for key, values, field in (("port", (65536, 0, -1, 70000, True, "6514", 6514.5), "format.port"),
                               ("facility", (24, -1, True), "format.facility"),
                               ("severity", ({"info": 8, "warn": 4, "error": 3},), "format.severity")):
        got = {repr(v): refused("syslog", fmt(key, v), field)[2] for v in values}
        check(f"syslog {field} out of its range ({', '.join(got)}) is refused, naming it", all(got.values()), got)
    for good in (1, 65535, 6514):
        check(f"...a port of {good} passes", not refused("syslog", fmt("port", good), "format.port")[0])
    for kind, key, v in (("ntfy", "priority", {"info": 3, "warn": 4, "error": 6}),
                         ("otlp", "severity", {"info": [25, "INFO"], "warn": [13, "WARN"], "error": [17, "ERROR"]}),
                         ("ntfy", "severity_tags", {"warn": ["a b"]})):
        check(f"{kind} format.{key} out of its range is refused, naming it", refused(kind, fmt(key, v),
                                                                                    f"format.{key}")[2])

    # a quote or a backslash in a value that lands in a Swift string
    for kind, key, v, name in (("webhook", "type_prefix", 'io.x"; let pwned = "\\', "cloudEventsTypePrefix"),
                               ("syslog", "sd_id", 'metacomputer@32473" x="\\', "syslogSDID"),
                               ("syslog", "scheme", 'syslog+tls"\\', "syslogScheme"),
                               ("otlp", "severity", {"info": [9, 'IN"FO\\'], "warn": [13, "WARN"],
                                                     "error": [17, "ERROR"]}, "otlpSeverity")):
        bad, made, ok = refused(kind, fmt(key, v), f"format.{key}")
        check(f"{kind} format.{key} with a quote and a backslash is refused, naming it", ok, (bad, made))
        with patched(kind, fmt(key, v)):
            swift = outcome(vault.swift)
        lit = swift_literal(swift[1], name) if swift[0] == "ok" else None
        if isinstance(v, str):
            good = swift_decode(lit) == v
        else:                       # [... "info": (9, "..."), ...]: the text, then the tuple's )
            value, rest = swift_prefix((lit or "").partition('"info": (9, ')[2])
            good = value == v["info"][1] and rest.startswith("), ")
        check(f"...and the vault's Swift, handed it past the check, holds it as one literal of exactly that value "
              f"({name})", good, (swift, lit))
    for kind, key, v in (("webhook", "content_type", "application/json\r\nX-Injected: 1"),
                         ("ntfy", "content_type", "application/json; x=\"a b\""),
                         ("webhook", "content_type", "text")):
        check(f"a content type that isn't TYPE/SUBTYPE[; NAME=VALUE] ({v!r}) is refused (it's a header's value)",
              refused(kind, fmt(key, v), "format.content_type")[2])

    # CODEOWNERS
    for owners in (["@x\n* @attacker"], ["@x /"], ["jon"], ["@x\r@y"], []):
        bad, made, ok = refused("webhook", top("owners", owners), "'owners'")
        check(f"owners {owners!r} are refused, naming the kind and 'owners'", ok, (bad, made))
    with patched("webhook", top("owners", ["@x\n* @attacker"])):
        block = outcome(gen.codeowners_block)
    check("...and gen-sinks' CODEOWNERS writer, handed one past the check, adds no line for another path (it refuses)",
          not codeowners_problems(block, "/sinks/webhook/"), block)

    # the docs: a line break, a cell, HTML, a link
    for about, why in (("sends\n## Injected\n| a | b |", "a line break"),):
        check(f"an about with {why} is refused, naming 'about'", refused("webhook", top("about", about), "'about'")[2])
    orig_md = gen.read("docs/sinks.md")
    for about, why in (("sends | <img src=x onerror=alert(1)> [x](javascript:alert(1)) `c` \\ end",
                        "| HTML, a link, a backtick and a backslash, on one line (the check takes it: it's text)"),
                       ("sends\n## Injected\n| a | b |", "a line break, past the check")):
        with patched("webhook", top("about", about)):
            md = outcome(gen.docs_index)
        check(f"gen-sinks' docs/sinks.md, with an about of {why}, keeps its table: no line, no cell, no HTML or link "
              "added", md[0] == "ok" and not md_problems(md[1], orig_md),
              md if md[0] != "ok" else md_problems(md[1], orig_md))
    for label in ("OTLP\n", "OT\x1bLP", "x" * 65):
        check(f"a label {label[:12]!r} is refused, naming 'label'", refused("otlp", top("label", label), "'label'")[2])


# --- the providers (lib/cloud; scripts/gen-providers.py) -----------------------------------------------------------
def providers(check):
    print("\nhostile provider manifests (every value that reaches generated shell, docs, CODEOWNERS)")
    import cloud
    from cloud import capabilities as caps
    from cloud import manifests as mf
    gen = script("gen-providers")
    name = cloud.registry.PROVIDERS[0]
    mod = cloud.registry.manifest(name)
    real_manifest, real_cli = mod.MANIFEST, getattr(mod, "CLI", None)

    @contextlib.contextmanager
    def patched(manifest=None, cli=None):
        mod.MANIFEST = dict(real_manifest, **(manifest or {}))
        mod.CLI = dict(real_cli, **(cli or {}))
        try:
            yield
        finally:
            mod.MANIFEST, mod.CLI = real_manifest, real_cli

    def nested(key, sub, value):
        return {key: dict(real_manifest[key], **{sub: value})}

    for change, field in (({"label": "Evil\nCloud"}, "label"), ({"label": "Evil\x1b[2JCloud"}, "label"),
                          ({"place": "x y"}, "place"), ({"place": "x;y"}, "place"), ({"name": "x$(y)"}, "name"),
                          ({"sizes": ("a b",)}, "sizes"), ({"regions": ("`r`",)}, "regions"),
                          (nested("billing", "note", "billed\n## Injected"), "billing"),
                          (nested("input", "note", "keys\x1b]52;c;x\x07"), "input")):
        bad = caps.check_manifest(dict(real_manifest, **change))
        check(f"a provider's MANIFEST {field} of {str(list(change.values())[0])[:30]!r} is refused, naming {field}",
              named(bad, field), bad)
        with patched(manifest=change):
            made = outcome(gen.wanted)
        check("...and gen-providers makes nothing from it, naming the provider and the field",
              made[0] == "refused" and f"providers/{name}/manifest.py" in made[1] and field in made[1], made)
    for change, field in (({"docs": "docs/../../x.md"}, "docs"), ({"docs": "docs/x.md)[e](http://e"}, "docs"),
                          ({"words": {"status_network": "net\r: > " + MARK}}, "words"),
                          ({"words": {"instance_word": "VM\x1b[2J"}}, "words"),
                          ({"credential_words": "its \x1b[31mkey"}, "credential_words"),
                          ({"place_words": "x y"}, "place_words"),
                          ({"owners": ("@x\n* @attacker",)}, "owners"),
                          ({"create_options": (("--zone", "a zone\n: > " + MARK, "", True),)}, "create_options")):
        with patched(cli=change):
            bad = mf.check_cli(name)
            made = outcome(gen.wanted)
        check(f"a provider's CLI {field} of {str(list(change.values())[0])[:30]!r} is refused by check_cli and by "
              f"gen-providers, naming the provider and {field}", named(bad, field) and made[0] == "refused"
              and f"providers/{name}/manifest.py" in made[1] and field in made[1], (bad, made))

    # the shell: a value with a line break, a quote and shell metacharacters, handed to the writer past the check
    hostile = "it's\n$(: > " + MARK + "1) `: > " + MARK + "2`; : > " + MARK + "3 'x' \"y\" \\z {egress}"
    real_cli_fn, real_manifest_fn = mf.cli, mf.manifest
    mf.cli = lambda n: dict(real_cli_fn(n), words=dict(real_cli_fn(n)["words"], status_network=hostile),
                            credential_words=hostile) if n == name else real_cli_fn(n)
    mf.manifest = lambda n: dict(real_manifest_fn(n), label=hostile) if n == name else real_manifest_fn(n)
    try:
        sh = outcome(gen.providers_sh)
        md = outcome(gen.docs_index)
    finally:
        mf.cli, mf.manifest = real_cli_fn, real_manifest_fn
    if sh[0] == "ok":
        runs, ran = sh_run(sh[1], [f"mc_pv {name} status_network", f"mc_pv {name} label",
                                   f"mc_pv {name} credential_words"])
    else:
        runs, ran = [], True
    check("gen-providers' lib/cloud/providers.sh, handed words with a line break, quotes and shell metacharacters past "
          "the check, runs nothing when sourced, and mc_pv gives each back exactly", sh[0] == "ok" and not ran
          and runs == [(0, hostile)] * 3, (sh[0], ran, runs))
    check("...and its docs/providers.md keeps its table and lines: no line, no cell, no HTML or link added",
          md[0] == "ok" and not md_problems(md[1], gen.read("docs/providers.md")),
          md if md[0] != "ok" else md_problems(md[1], gen.read("docs/providers.md")))
    mf.manifest = lambda n: dict(real_manifest_fn(n), label="Cloud | <img src=x> [x](javascript:alert(1))",
                                 sizes=("s|`x`",)) if n == name else real_manifest_fn(n)
    try:
        md = outcome(gen.docs_index)
    finally:
        mf.manifest = real_manifest_fn
    check("...as with a |, HTML, a link and a backtick on one line (a label, a size)",
          md[0] == "ok" and not md_problems(md[1], gen.read("docs/providers.md")),
          md if md[0] != "ok" else md_problems(md[1], gen.read("docs/providers.md")))
    mf.cli = lambda n: dict(real_cli_fn(n), owners=("@x\n* @attacker",)) if n == name else real_cli_fn(n)
    try:
        block = outcome(gen.codeowners_block)
    finally:
        mf.cli = real_cli_fn
    check("...and its CODEOWNERS writer, handed an owner with a line break past the check, adds no line for another "
          "path (it refuses)", not codeowners_problems(block, f"/providers/{name}/"), block)


# --- the coding agents (lib/agentkit; scripts/gen-agents.py, lib/agentkit/recognition.py) ---------------------------
def agents(check):
    print("\nhostile agent manifests and recognition tables (every value that reaches generated shell, the vault's "
          "Swift, CODEOWNERS, docs)")
    from agentkit import manifests, recognition, registry
    gen = script("gen-agents")
    real = registry.data
    name = registry.DEFAULT
    flag = manifests.manifest(name)["flag"]

    @contextlib.contextmanager
    def patched(part, change, who=name):
        raw = copy.deepcopy(real(who, part))
        change(raw)
        registry.data = lambda n, p: raw if (n, p) == (who, part) else real(n, p)
        try:
            yield
        finally:
            registry.data = real

    def top(key, value):
        return lambda raw: raw.__setitem__(key, value)

    def inst(key, value):
        return lambda raw: raw.setdefault("install", {}).__setitem__(key, value)

    for change, field, what in ((top("owners", ["@x\n* @attacker"]), "'owners'", "an owner with a line break"),
                                (top("owners", ["@x /"]), "'owners'", "an owner with a space"),
                                (top("label", "Evil\n$(: > " + MARK + ")"), "'label'", "a label with a line break"),
                                (top("label", "Evil\x1b[2J"), "'label'", "a label with an escape"),
                                (top("short", "x\ny"), "'short'", "a short name with a line break"),
                                (top("tools", "x\ny"), "'tools'", "tools with a line break"),
                                (top("prog", "a b"), "'prog'", "a command with a space"),
                                (top("prog", "$(id)"), "'prog'", "a command with $( )"),
                                (top("json_key", "a-b c"), "'json_key'", "a JSON key with a space"),
                                (top("docs", "docs/../../x.md"), "'docs'", "a docs page with .."),
                                (top("docs", "docs/x.md)[e](http://e"), "'docs'", "a docs page with a link"),
                                (top("release_file", ".."), "release_file", "a release file of .."),
                                (top("bench", {"switch": "x; y"}), "'bench'", "a bench word with ;"),
                                (inst("options", ["--x; : > " + MARK]), "install.options", "an option with ;"),
                                (inst("options", ["*"]), "install.options", "an option of *"),
                                (inst("options", ["--ok\n--evil"]), "install.options", "an option with a line break"),
                                (inst("only_note", "Done.\n: > " + MARK), "install.only_note",
                                 "an only_note with a line break"),
                                (inst("switch", "x\x1b[2J"), "install.switch", "a switch with an escape")):
        with patched("manifest", change):
            bad = manifests.check(name)
            made = outcome(gen.wanted)
        check(f"{what} is refused by the check and by gen-agents, naming the agent and the field ({field})",
              named(bad, field) and made[0] == "refused" and f"coding-agents/{name}/manifest.json" in made[1]
              and field in made[1], (bad, made))

    # the shell: values with a line break, quotes and shell metacharacters, handed to the writer past the check
    hostile = "it's\n$(: > " + MARK + "1) `: > " + MARK + "2`; : > " + MARK + "3 'x' \"y\" \\z"

    def all_hostile(raw):
        raw["label"] = hostile
        raw.setdefault("install", {})["only_note"] = hostile
    with patched("manifest", all_hostile):
        sh = outcome(gen.agents_sh)
        md = outcome(gen.docs_index)
    runs, ran = sh_run(sh[1], [f"mc_agent {flag} label", f"mc_agent {flag} only_note"]) if sh[0] == "ok" else ([], True)
    check("gen-agents' lib/agentkit/agents.sh, handed a label and an only_note with a line break, quotes and shell "
          "metacharacters past the check, runs nothing when sourced, and mc_agent gives each back exactly",
          sh[0] == "ok" and not ran and runs == [(0, hostile)] * 2, (sh[0], ran, runs))
    orig_md = gen.read("docs/agents.md")
    check("...and its docs/agents.md keeps its table and lines: no line, no cell, no HTML or link added",
          md[0] == "ok" and not md_problems(md[1], orig_md), md if md[0] != "ok" else md_problems(md[1], orig_md))
    with patched("manifest", top("tools", "x` | <img src=x onerror=alert(1)> | [x](javascript:alert(1)) `y")):
        ok_check = manifests.check(name)
        md = outcome(gen.docs_index)
    check("tools with backticks, |, HTML and a link on one line (the check takes it: it's text) keep docs/agents.md's "
          "table: the code span holds them", not ok_check and md[0] == "ok" and not md_problems(md[1], orig_md),
          (ok_check, md if md[0] != "ok" else md_problems(md[1], orig_md)))
    with patched("manifest", top("owners", ["@x\n* @attacker"])):
        block = outcome(gen.codeowners_block)
    check("...and its CODEOWNERS writer, handed an owner with a line break past the check, adds no line for another "
          "path (it refuses)", not codeowners_problems(block, f"/coding-agents/{name}/"), block)

    # the recognition tables, which the vault's build compiles in (Recognition.swift)
    label = manifests.manifest(name)["label"]
    for change, field, what in ((lambda r: r["kinds"].append("Evil\nKind"), "kinds", "a kind with a line break"),
                                (lambda r: r["kinds"].append("Evil\x1b[2J"), "kinds", "a kind with an escape"),
                                (lambda r: r.setdefault("executables", {}).__setitem__("ev\nil", label), "executables",
                                 "an executable's name with a line break"),
                                (lambda r: r.setdefault("names", {}).__setitem__("ev\x00il", label), "names",
                                 "a name with a NUL"),
                                (lambda r: r.setdefault("shared", {}).__setitem__(label, {
                                    "label": "Evil\n", "under": "x"}), "label", "a shared label with a line break"),
                                (lambda r: r.setdefault("shared", {}).__setitem__(label, {
                                    "label": "x", "under": "y\x1b]52;c;x\x07"}), "under", "a question's words with an "
                                                                                          "escape"),
                                (lambda r: r.setdefault("shared", {}).__setitem__(label, {
                                    "label": "x", "under": "y", "within": True}), "within", "a within of true"),
                                (lambda r: r.setdefault("shared", {}).__setitem__(label, {
                                    "label": "x", "under": "y", "subcommands": ["a b"]}), "subcommands",
                                 "a subcommand with a space")):
        with patched("recognition", change):
            bad = recognition.check()
            sw = quiet(recognition.main, ["swift"])
        check(f"{what} in recognition.json is refused (recognition.py check, naming the agent and {field}), and "
              "recognition.py swift writes nothing (exit 1)", named(bad, f"{name}:", field) and sw == ("ok", 1),
              (bad, sw))

    # a quote or a backslash in what lands in a Swift string (the tables' labels): one literal of exactly that value
    quoted = 'Evil "Agent\\" \\u{41}'

    def quote_label(raw):
        raw["kinds"] = [quoted if k == label else k for k in raw["kinds"]]
        for table in recognition.WORD_TABLES:
            for w, k in list((raw.get(table) or {}).items()):
                if k == label:
                    raw[table][w] = quoted
        for s in (raw.get("shared") or {}).values():
            s["under"] = quoted
    real_data = registry.data

    def both(n, p):
        d = copy.deepcopy(real_data(n, p))
        if n == name and p == "manifest":
            d["label"] = quoted
        elif n == name and p == "recognition":
            quote_label(d)
        return d
    registry.data = both
    try:
        ok_check = recognition.check()
        swift = outcome(recognition.swift)
    finally:
        registry.data = real_data
    lit = swift_literal(swift[1], "recordDefaultAgent") if swift[0] == "ok" else None
    check("a label with a quote and backslashes (one line: the check takes it) is one Swift literal of exactly that "
          "value in the vault's tables (recordDefaultAgent)", not ok_check and swift_decode(lit) == quoted,
          (ok_check, lit))
    build_refuses(check, "a recognition.json kind with a line break", f"coding-agents/{name}/recognition.json",
                  lambda d: d["kinds"].append("Evil\nKind"), (f"{name}: kinds",))
