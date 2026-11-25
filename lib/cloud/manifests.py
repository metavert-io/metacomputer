"""What the command line needs from each provider, read from its manifest (providers/<name>/manifest.py `CLI`), so no
shared program names a provider: its credentials, the settings and options it takes, its transport, and the words
its messages use. Each provider's capabilities are its MANIFEST (lib/cloud/capabilities.py); this is the rest.

A provider's CLI is a dict, checked by check_cli():
    owners             who reviews its folder (.github/CODEOWNERS)
    place_words        how `--on` names it in a list of places ("PLACE (ITS LABEL)"; default: its place)
    docs               its page for people (default docs/remote.md)
    credentials        the environment variables its credentials come in: taken off every program's environment but
                       lib/remote.py's (none: it keeps its own sign-in)
    not_secret         those that aren't secrets (a region's name may be in a message)
    credential_words   how a message names them (default: the variables, by name)
    ask_needs          the variables that must be set for it to be asked from here (default: credentials); a provider
                       whose providers/<name>/cli.sh has pv_<name>_can_ask decides that itself
    python             the setting naming a Python that has its SDK, which lib/remote.py runs with (or None)
    copy_settings      settings a new computer's file copies from this checkout's (default: python's)
    api_host           the one host its REST API is reached at (lib/provider_http.py; or None)
    transport          the SSH transport it registers (lib/cloud/transport.py), when it isn't plain ssh
    transport_settings the settings that transport reads, passed to lib/remote.py target, tunnel and probe
    create_options     (OPTION, WHAT ITS VALUE IS, ITS DEFAULT'S ENVIRONMENT VARIABLE or "", ONLY FOR IT) for `vm
                       create`'s options of its own: its value is the variable opt_NAME there (--org: opt_org), and
                       one that's only for it is refused for any other place
    cloud_options      (OPTION, ENVIRONMENT DEFAULT) for `cloud ...`'s options of its own
    teardown           whether `cloud teardown` has anything to remove there
    idle_stop_default  IDLE_STOP for a new computer when --idle-stop isn't given (default 15)
    words              {KEY: text}: the provider's own words for the messages that name SHELL_WORDS' keys
Only lib/cloud and scripts/gen-providers.py read it; bash reads it through lib/cloud/providers.sh (generated). It's a
contributor's input (a pull request may touch its provider's folder only), and its values reach generated shell, docs
and CODEOWNERS: check_cli() holds each to a whitelist of its own (lib/gensafe.py), and scripts/gen-providers.py makes
nothing until every provider's passes it and capabilities.check_manifest."""
from . import registry

KEYS = {"owners", "place_words", "docs", "credentials", "not_secret", "credential_words", "ask_needs", "python",
        "copy_settings", "api_host", "transport", "transport_settings", "create_options", "cloud_options", "teardown",
        "idle_stop_default", "words"}
# The words a provider may give for shared messages, each in place of the shared one:
#   vm_create_confirm  vm create's question before it makes the computer
#   instance_word      what vm delete calls the computer at the provider ("instance" otherwise)
#   status_baseline    status's baseline line (none otherwise)
#   status_network     status's network line ({egress}: the setting), instead of its own firewall's
#   status_uinput      added to status's driver line
SHELL_WORDS = ("vm_create_confirm", "instance_word", "status_baseline", "status_network", "status_uinput")


def manifest(name):
    return registry.manifest(name).MANIFEST


def cli(name):
    """The provider's CLI dict with its defaults filled in."""
    raw = dict(getattr(registry.manifest(name), "CLI", {}) or {})
    m = manifest(name)
    creds = tuple(raw.get("credentials") or ())
    out = {"owners": tuple(raw.get("owners") or ()), "place_words": raw.get("place_words") or m["place"],
           "docs": raw.get("docs") or "docs/remote.md", "credentials": creds,
           "not_secret": tuple(raw.get("not_secret") or ()),
           "credential_words": raw.get("credential_words") or (" ".join(creds) or "its credentials"),
           "ask_needs": tuple(raw.get("ask_needs") or creds), "python": raw.get("python"),
           "copy_settings": tuple(raw.get("copy_settings") or ((raw["python"],) if raw.get("python") else ())),
           "api_host": raw.get("api_host"), "transport": raw.get("transport"),
           "transport_settings": tuple(raw.get("transport_settings") or ()),
           "create_options": tuple(tuple(o) for o in raw.get("create_options") or ()),
           "cloud_options": tuple(tuple(o) for o in raw.get("cloud_options") or ()),
           "teardown": raw.get("teardown", True), "idle_stop_default": raw.get("idle_stop_default"),
           "words": dict(raw.get("words") or {})}
    return out


def check_cli(name):
    """The problems with a provider's CLI dict, as a list of strings (empty: it's sound). Each names its field."""
    import re
    import gensafe              # lib/gensafe.py: only checks need it
    if not (isinstance(name, str) and registry.PART.fullmatch(name)):
        return [f"the provider's name {name!r} isn't a lowercase word (a letter, then letters, digits and _)"]
    raw = getattr(registry.manifest(name), "CLI", None)
    if not isinstance(raw, dict):
        return ["CLI: a dict"]
    bad = [f"CLI: unknown key {k}" for k in raw if k not in KEYS]
    c = cli(name)
    env = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
    opt = re.compile(r"--[a-z][a-z0-9-]{1,30}")
    if gensafe.owners(c["owners"]):
        bad.append("CLI owners: one or more GitHub @names (@login or @org/team)")
    for k in ("credentials", "not_secret", "ask_needs", "copy_settings", "transport_settings"):
        if not all(isinstance(v, str) and env.fullmatch(v) for v in c[k]):
            bad.append(f"CLI {k}: environment variables' names")
    if not set(c["not_secret"]) <= set(c["credentials"]):
        bad.append("CLI not_secret: only names of its credentials")
    if c["python"] is not None and not env.fullmatch(str(c["python"])):
        bad.append("CLI python: a setting's name")
    if c["transport"] is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", str(c["transport"])):
        bad.append("CLI transport: a transport's name")
    if c["transport_settings"] and not c["transport"]:
        bad.append("CLI transport_settings: only with a transport of its own")
    for o in c["create_options"]:
        if not (len(o) == 4 and opt.fullmatch(str(o[0])) and not gensafe.text(o[1], 120)
                and (o[2] == "" or env.fullmatch(str(o[2]))) and isinstance(o[3], bool)):
            bad.append(f"CLI create_options: (OPTION, WHAT ITS VALUE IS, DEFAULT'S VARIABLE or \"\", ONLY FOR IT), "
                       f"not {o!r}")
    shared = {"--on", "--host", "--user", "--port", "--size", "--region", "--egress", "--idle-stop", "--ssh-port",
              "--vnc-port", "--no-build", "--yes", "--confirm", "--json", "--forget"}
    bad += [f"CLI: {o[0]} is one of the shared options" for o in c["create_options"] + c["cloud_options"]
            if o[0] in shared]
    for o in c["cloud_options"]:
        if not (len(o) == 2 and opt.fullmatch(str(o[0])) and env.fullmatch(str(o[1]))):
            bad.append(f"CLI cloud_options: (OPTION, ENVIRONMENT VARIABLE), not {o!r}")
    if c["idle_stop_default"] is not None and not re.fullmatch(r"off|[1-9][0-9]{0,2}", str(c["idle_stop_default"])):
        bad.append("CLI idle_stop_default: minutes (1-999), or off")
    if not isinstance(c["teardown"], bool):
        bad.append("CLI teardown: True or False")
    if c["api_host"] is not None and not re.fullmatch(r"[a-z0-9.-]{3,253}", str(c["api_host"])):
        bad.append("CLI api_host: a host name")
    bad += [f"CLI words: unknown key {k}" for k in c["words"] if k not in SHELL_WORDS]
    bad += [f"CLI words: {k} isn't {gensafe.text(v, 400)}" for k, v in c["words"].items() if gensafe.text(v, 400)]
    for k in ("place_words", "credential_words"):
        if gensafe.text(c[k], 120):
            bad.append(f"CLI {k}: {gensafe.text(c[k], 120)}")
    if gensafe.docs_page(c["docs"]):
        bad.append(f"CLI docs: {gensafe.docs_page(None)}")
    return bad


def _union(key):
    out = []
    for name in registry.PROVIDERS:
        for v in cli(name)[key]:
            if v not in out:
                out.append(v)
    return tuple(out)


def credentials():
    """Every provider's credential variables, in the registry's order: taken off every environment but lib/remote.py's."""
    return _union("credentials")


def not_secret():
    return _union("not_secret")


def copy_settings():
    return _union("copy_settings")


def transport_settings():
    return _union("transport_settings")


def create_options():
    """Every provider's own vm create options: (OPTION, WHAT ITS VALUE IS, DEFAULT'S VARIABLE, ONLY FOR IT, PROVIDER),
    in the registry's order (an option two providers declare is listed once, as the first declares it)."""
    out, seen = [], set()
    for name in registry.PROVIDERS:
        for o in cli(name)["create_options"]:
            if o[0] not in seen:
                seen.add(o[0])
                out.append((*o, name))
    return tuple(out)


def cloud_options():
    """Every provider's own cloud options (OPTION, ENVIRONMENT VARIABLE), in the registry's order: an option two
    providers declare is listed once, as the first declares it (option_conflicts() names any they declare
    differently), so the command line never takes one twice."""
    out, seen = [], set()
    for name in registry.PROVIDERS:
        for o in cli(name)["cloud_options"]:
            if o[0] not in seen:
                seen.add(o[0])
                out.append(o)
    return tuple(out)


def option_conflicts():
    """The options two providers declare differently (their create_options or cloud_options), as a list of strings
    (empty: none). Only the first one's would be used, so the lint refuses them."""
    bad = []
    for key in ("create_options", "cloud_options"):
        first = {}
        for name in registry.PROVIDERS:
            for o in cli(name)[key]:
                if o[0] in first and first[o[0]][1] != o:
                    bad.append(f"{key}: {o[0]} is {first[o[0]][1]!r} in {first[o[0]][0]} but {o!r} in {name}")
                first.setdefault(o[0], (name, o))
    return bad


def api_hosts():
    """The hosts the providers' REST APIs are reached at (lib/provider_http.py's allowlist)."""
    return tuple(h for h in (cli(n)["api_host"] for n in registry.PROVIDERS) if h)


def by_transport(transport):
    """The provider whose transport `transport` is (its name), or None for plain ssh or one no provider registers."""
    for name in registry.PROVIDERS:
        if cli(name)["transport"] == transport:
            return name
    return None


def labels_where(test):
    """The labels of the providers whose MANIFEST passes `test`, joined as a list in words ("A", "A and B")."""
    labels = [manifest(n)["label"] for n in registry.PROVIDERS if test(manifest(n))]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1] if labels else ""


def shell_words(name):
    """(KEY, VALUE) pairs of provider `name` for lib/cloud/providers.sh's mc_pv."""
    m, c = manifest(name), cli(name)
    out = [("label", m["label"]), ("place", m["place"]), ("place_words", c["place_words"]),
           ("label_place", f"{m['label']} ({m['place']})"), ("docs", c["docs"]),
           ("credential_words", c["credential_words"]), ("ask_needs", " ".join(c["ask_needs"])),
           ("background_input", "on" if m["input"]["background"] else "off"),
           ("teardown", "yes" if c["teardown"] else "no")]
    for k in ("python", "transport", "idle_stop_default"):
        if c[k]:
            out.append((k, c[k]))
    out += [(k, c["words"][k]) for k in SHELL_WORDS if k in c["words"]]
    return out
