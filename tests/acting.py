"""What every program of the repository, and each of its subcommands, does to a computer: the table
tests/test_acting.py reads (and nothing else may say otherwise).

While the user has the computer (a handoff: lib/handoff.py), every entry that changes it must wait, however it's run:
by bin/metacomputer, by another program, or by itself. Changing a computer means its power state, its disks, its
snapshots or checkpoints, its network rules, the guest's contents or processes, or creating or deleting it. Every
program tests/entrypoints.py finds (and every command of bin/metacomputer's dispatch) is here, with each of its
subcommands; one that isn't classified fails the test, so a new one must be.

The labels:
  ACTS      changes a computer itself: waits (exit 4, saying the user has it) before it calls, runs or writes anything,
            except at the user's own terminal or under a hold its caller handed on (Python: lib/handoff_gate.py; bash:
            scripts/lib.sh script_handoff_hold, bin/metacomputer handoff_hold). Each valid line is run while a handoff
            is open, at the user's terminal and under an inherited hold.
  LIFECYCLE bin/metacomputer's lifecycle commands (and a computer elsewhere's start, stop and network) and the
            vault's fills: they wait (exit 4) even at the user's own terminal. A lifecycle command's `--override` (the
            user's own say-so) goes ahead, as it does for another session, holding the lock for what it runs
            (SECURITY.md, "What it is and isn't"); a fill has none: it waits until the handoff ends.
  SHARED    changes what several computers use (a volume: the computers whose checkpoints keep it; a provider's network,
            when its adapter says its setup rewrites it: every computer of this checkout's there): waits while any of
            them has a handoff open (lib/handoff_gate.py wait_or_hold_all, in path order), and refuses when one of them
            can't be told. tests/test_acting.py sets up such computers for each (SHARED_FIXTURES).
  RESET     an input reset (lib/input_reset.py): under the dispatch lock held exclusively; while a handoff is open only
            at the user's own terminal (exit 4, or `view --open`'s silent nothing), never under another's hold.
  USER      only at the user's own terminal, whatever any handoff says (it may act on any computer at a provider; or
            it's metacomputer's own, which only a build or the user runs).
  PER_CALL  judges each call or run where it leaves the Mac, under the dispatch lock (the hub, the relay, the shell
            channel: tests/test_hub.py, tests/test_handoff.py): acting ones wait, reads go on.
  THROUGH   changes a computer only through other entries, named in its note, each of which waits on its own (or runs
            only the command its caller hands it, after `--`).
  ONGOING   an allowlist's own end at its time (netpolicy _expire, started by the allow that waited): never a change
            anyone makes now, and never holding the lock for hours. (The folder watcher is ACTS: it looks once when it
            starts, the hold of the start that starts it handed on, and holds nothing while it runs.)
  GUEST     runs inside the computer; reached only through an entry on the Mac that waits.
  NONE      changes no computer: reads one, or works only on this Mac (its settings, records, the vault, files, the
            repository) or at a provider's account level.
  SOURCED   a library of functions (scripts/lib.sh, scripts/vm-commands.sh): what it does belongs to bin/metacomputer's
            commands and the scripts that source it.

A program's entry is a label, or {SUBCOMMAND: label}, where a label may be a function of the line's words (a form
that differs). The subcommand is the line's first word (two for a group: `volume sync`), after a `--transport N
WORD...` the program takes first.
"""
ACTS, LIFECYCLE, SHARED, RESET, USER, PER_CALL, THROUGH, ONGOING, GUEST, NONE, SOURCED = (
    "acts", "lifecycle", "shared", "reset", "user", "per-call", "through", "ongoing", "guest", "none", "sourced")
LABELS = (ACTS, LIFECYCLE, SHARED, RESET, USER, PER_CALL, THROUGH, ONGOING, GUEST, NONE, SOURCED)
G, R, S = ("provisioning/guest/rootfs/opt/metacomputer/bin/", "provisioning/remote/rootfs/opt/metacomputer/bin/",
           "providers/sprites/guest/rootfs/opt/metacomputer/sprite/")


def has(*words):
    return lambda row: any(w in row for w in words)


def when(test, yes, no):
    """A label decided by the line's words: YES when TEST(row) holds, else NO."""
    return lambda row: yes if test(row) else no


PROGRAMS = {
    # bin/metacomputer: its dispatch's commands are BIN, below
    "bin/metacomputer": "BIN",
    # the launchers hand every command to an installed checkout's bin/metacomputer (or setup to get.sh)
    "agent-plugin/bin/metacomputer": THROUGH,
    "agent-plugin/bin/metacomputer#uninstalled": NONE,
    "plugin/bin/metacomputer": THROUGH,
    "plugin/bin/metacomputer#setup": THROUGH,              # get.sh -> install.sh -> metacomputer build
    "plugin/bin/metacomputer#uninstalled": NONE,
    "get.sh": THROUGH,                                      # install.sh
    "plugin/get.sh": THROUGH,
    "install.sh": THROUGH,                                  # metacomputer build (scripts/build-vm.sh holds it)
    "scripts/build-tools.sh": NONE,
    "scripts/build-vault.sh": NONE,
    "scripts/build-vm.sh": ACTS,                            # script_handoff_hold, for the whole build
    "scripts/check-public.sh": NONE,
    "scripts/fetch-artifacts.sh": NONE,
    "scripts/lib.sh": SOURCED,
    "scripts/vm-commands.sh": SOURCED,
    "scripts/pin-gpg-key.sh": NONE,
    "scripts/release.sh": NONE,
    "scripts/remote-build.sh": ACTS,                        # script_handoff_hold, for the whole build
    "scripts/safe-rmtree.sh": NONE,                         # this Mac's folders (never a VM's)
    "scripts/sync-plugin.py": NONE,
    "scripts/gen-providers.py": NONE,                      # files of this checkout, made from the manifests
    "scripts/gen-agents.py": NONE,                         # files of this checkout, made from the agents' manifests
    "scripts/build-openai-plugin.py": NONE,
    "scripts/vendor-driver-skill.py": NONE,
    "scripts/verify-audit.py": NONE,
    "tests/agentic_claude.sh": THROUGH,                     # a headless session: its tools through the hub
    "tests/run_tests.py": THROUGH,                          # the hub, and metacomputer status
    "tests/skill/analyze.py": NONE,
    "tests/skill/run_headless.sh": THROUGH,                 # a headless session: its tools through the hub
    "bench/bench.py": {"list": NONE, "export": NONE, "report": NONE, "compare": NONE, "remetric": NONE,
                       "run": THROUGH, "check": THROUGH},   # metacomputer reset, put, ssh; sessions through the hub
    "bench/guest/calc-overlay.sh": GUEST,
    "bench/guest/server.py": GUEST,
    "lib/actionlog.py": NONE,
    "lib/agentkit/recognition.py": NONE,                  # the agents' recognition tables, printed
    "lib/agent_setup.py": NONE,
    "lib/agents.py": NONE,
    "coding-agents/codex/setup.py": NONE,                 # Codex's MCP server, read (codex mcp get)
    "lib/botcheck.py": NONE,
    # the hub, metacomputer sh, ssh, task, get, and its cleanup under owners.acting()
    "lib/browser_tools.py": {k: THROUGH for k in ("compare", "responsive", "a11y", "open-untrusted", "gpu", "debug",
                                                  "read", "page")},
    "lib/claude_mcp.py": NONE,
    "lib/defaultvm.py": NONE,
    "lib/driver_profiles.py": NONE,
    "lib/each.py": THROUGH,                                 # metacomputer --vm NAME COMMAND, each holding its own
    "lib/events.py": NONE,
    "lib/guest_import.py": THROUGH,                         # the caller's command after `--` (a read of the guest)
    "lib/guest_inspect.py": GUEST,                          # (--render only formats the guest's answer on the Mac)
    "lib/guest_profiles.py": GUEST,
    "lib/guest_put.py": ACTS,
    # the handoff's own commands (open, done, cancel, wait, status, the guard, the dialog, the panel): the pause itself
    "lib/handoff.py": NONE,
    "lib/input_reset.py": {"view": RESET, "reset": when(has("--check"), NONE, RESET)},
    "lib/learn.py": {"report": NONE, "review": NONE, "approve": NONE, "reject": NONE, "remove": NONE, "edit": NONE,
                     "add": NONE, "reseal": NONE, "list": NONE, "bench": THROUGH},      # bench/bench.py
    "lib/liveview.py": NONE,                                # the Mac's keychain, clipboard and Screen Sharing
    "lib/mcomp_sdk.py": {"call": THROUGH, "probe": NONE, "tools": NONE, "describe": NONE},   # call: the hub
    "lib/mcp_client.py": {"--call": THROUGH, "--probe": NONE, "--tools": NONE, "--describe": NONE},
    "lib/mcp_hub.py": {"serve": PER_CALL, "start": PER_CALL, "stop": NONE, "status": NONE, "": NONE,
                       "refresh-cache": NONE},              # stop: SECURITY.md's accepted limit
    "lib/mcp_lazy.py": THROUGH,                             # the hub
    "lib/mcp_relay.py": PER_CALL,                           # each acting call under owners.acting()
    "lib/netpolicy.py": {"status": NONE, "allow": ACTS, "remove": ACTS, "clear": ACTS, "render": ACTS,
                         "_expire": ONGOING},
    "lib/owners.py": NONE,
    "lib/page_guest.py": GUEST,
    "lib/placement.py": NONE,
    "lib/proxywrap.py": THROUGH,                            # the caller's command (a provider's tunnel)
    # save starts the guest's recorder stream: it checks a --hold-fd it's handed is a hold, else looks for itself
    "lib/recording.py": {"sidecar": NONE, "finalize": NONE, "moments": NONE, "probe": NONE, "unsynced": NONE,
                         "save": ACTS, "capture": THROUGH},      # capture: the caller's command, a read of the guest
    "lib/remote.py": {
        "target": NONE, "tunnel": NONE, "probe": NONE, "missing": NONE, "status": NONE, "state": NONE, "rates": NONE,
        "user-data": NONE, "plan": NONE, "key-lists": NONE, "setup-done": NONE, "identity": NONE,   # this Mac's records
        "exec": ACTS, "create": ACTS, "wake": ACTS, "sleep": ACTS, "delete": ACTS, "abort": ACTS, "resolvers": ACTS,
        "checkpoint": when(has("create", "restore", "delete"), ACTS, NONE),
        "allow": when(has("--show"), NONE, ACTS),
        "egress": when(has("--set"), ACTS, NONE),
        "cloud": lambda row: (USER if ("teardown" in row or any(w.startswith("--forget") for w in row))
                              and "sprites" not in row else SHARED if "setup" in row else NONE)},
    "lib/safedelete.py": NONE,
    "lib/sessionvm.py": NONE,
    "lib/shop.py": NONE,
    "lib/storage.py": {
        "volume create": NONE, "volume keys": NONE, "volume test": NONE, "volume list": NONE,
        "volume delete": SHARED,                            # the snapshots computers' checkpoints keep of it
        "volume resize": NONE, "refdisk create": NONE, "refdisk list": NONE, "refdisk delete": NONE,
        "attached": NONE, "status": NONE,
        "volume sync": ACTS, "prepare": ACTS, "started": ACTS, "attach": ACTS, "release": ACTS, "kill-vm": ACTS,
        "checkpoint": when(has("save", "restore", "delete"), ACTS, NONE),
        "watch": ACTS},                 # long-running: it looks once when it starts and holds nothing (look_once)
    "lib/termsafe.py": NONE,
    "lib/traces.py": NONE,                                  # the hub records; the traces are files on the Mac
    "lib/tunnellog.py": THROUGH,                            # the caller's command (a tunnel)
    "lib/update.py": {"version": NONE, "_record-build": NONE, "_guest-digest": NONE, "_runinfo-json": NONE,
                      "_refresh-check": NONE, "_status-line": NONE,
                      "update": THROUGH, "_after-install": THROUGH},     # metacomputer stop, start, hub stop
    "lib/vm_claude.py": PER_CALL,                           # every step through vm_shell.run_guarded
    "lib/vm_claude_guest.sh": GUEST,
    "lib/vm_shell.py": PER_CALL,                            # run_guarded: exit 125 while the user has the computer
    "lib/vm_shell_guest.py": GUEST,
    "lib/vms.py": NONE,
    "tools/qcow2raw": NONE,                                 # a disk image file on the Mac, while a build holds it
}
for _p in ("provisioning/guest/finalize.sh", "provisioning/guest/provision.sh", "provisioning/remote/bootstrap.sh",
           "providers/sprites/guest/bootstrap.sh", G + "desktop-record", G + "driver-mcp", G + "guest-status",
           G + "input-reset", G + "secret-fill", G + "session-init", G + "session-run", G + "storage-helper",
           "provisioning/guest/rootfs/usr/local/sbin/metacomputer-admin", R + "guest-firewall", R + "idle-stop",
           R + "remote-uninstall", S + "hide-sprite-api", S + "in-session", S + "services", S + "svc-desktop",
           S + "svc-session", S + "svc-sshd", S + "svc-xvfb", S + "xvfb-inner"):
    PROGRAMS[_p] = GUEST
# Programs whose first words are a group's: the subcommand is the first two (`volume sync`).
GROUPS = {"lib/storage.py": ("volume", "refdisk")}
# Programs that take a `--transport N WORD...` first: the subcommand follows it.
TRANSPORT_FIRST = ("lib/vm_shell.py", "lib/vm_claude.py", "lib/guest_put.py")


def _secrets(args):
    """The vault's subcommands that fill or act in the computer (bin/metacomputer do_secrets: handoff_hold)."""
    sub = args[0] if args else "status"
    if sub in ("fill", "login", "code", "relay", "give") or (sub == "session" and args[1:2] == ["load"]):
        return LIFECYCLE
    if sub == "approve" or (sub == "pay" and not ("--purchase" in args and "--done" in args)):
        return LIFECYCLE
    return NONE


def _first(args):
    return args[0] if args else ""


# bin/metacomputer's commands: by command, or a function of its arguments (after the command) and of whether its
# computer is elsewhere (REMOTE: reached over SSH, whose start, stop and network are lifecycle commands there).
BIN = {
    "start": lambda a, remote=False: LIFECYCLE if remote else ACTS,
    "put": ACTS, "rename": ACTS, "ssh": ACTS,
    "_vnc-install": USER, "_vnc-push": USER,     # metacomputer's own: from a build, or at the user's own terminal
    "stop": LIFECYCLE, "restart": LIFECYCLE, "reset": LIFECYCLE, "restore": LIFECYCLE, "baseline": LIFECYCLE,
    "checkpoint": LIFECYCLE, "desktop-restart": LIFECYCLE, "test": LIFECYCLE,
    "build": ACTS,                                          # scripts/build-vm.sh, scripts/remote-build.sh
    "task": lambda a: ACTS if _first(a) == "start" else NONE,
    "browser": lambda a: ACTS if _first(a) == "profiles" and (a[1:2] in (["--new"], ["--delete"])) else NONE,
    "security": lambda a: ACTS if "apply" in a else NONE,
    "record": lambda a: ACTS if _first(a) in ("start", "stop") else NONE,
    "volume": lambda a: ACTS if _first(a) == "sync" else SHARED if _first(a) == "delete" else NONE,
    "refdisk": NONE,
    "net": lambda a, remote=False: ((LIFECYCLE if remote else ACTS) if _first(a) in ("allow", "remove", "clear", "egress")
                                    else NONE),
    "vm": lambda a: ACTS if _first(a) == "delete" else THROUGH if _first(a) == "create" else NONE,
    "cloud": lambda a: (USER if ("teardown" in a or any(w.startswith("--forget") for w in a)) and "sprites" not in a
                        else SHARED if _first(a) == "setup" else NONE),
    "secrets": _secrets,
    "input-reset": lambda a: NONE if _first(a) == "--check" else RESET,
    "view": lambda a: RESET if "--open" in a or not [w for w in a if w not in ("--remember", "--no-remember")]
                      else NONE,
    "sh": PER_CALL, "claude": PER_CALL,
    # mcp: per call through the relay; its start, with DRIVER_MODE=bounded and no manifest in the guest yet, writes one
    # under handoff_hold_unless_user first (tests/test_acting.py mcp_manifest_checks)
    "mcp": PER_CALL,
    "hub": lambda a: PER_CALL if _first(a) in ("start", "serve") else NONE,
    "call": THROUGH, "web": THROUGH, "mcp-lazy": THROUGH, "each": THROUGH, "update": THROUGH, "setup": THROUGH,
    "handoff": NONE,
}
for _c in ("status", "who", "where", "inspect", "logs", "get", "shot", "contact-sheet", "checkpoints", "recordings",
           "list", "default", "events", "notify", "learn", "actions", "agents", "version", "help", "-h", "--help",
           "trace", "release", "shop", "_health", "_runinfo", "_task-sync"):
    BIN[_c] = NONE


def subcommand(prog, row):
    """The subcommand a line of PROG is (its first word, or two for a group), after a leading --transport N WORD..."""
    words = list(row)
    if prog in TRANSPORT_FIRST and words[:1] == ["--transport"] and len(words) > 1 and words[1].isdigit():
        words = words[2 + int(words[1]):]
    if prog == "lib/mcp_client.py":
        return words[0] if words else ""        # its forms start with an option: --call, --probe, --tools, --describe
    first = words[0] if words and not words[0].startswith("-") else ""
    if first and first in GROUPS.get(prog, ()):
        return f"{first} {words[1] if len(words) > 1 else ''}"
    return first


def classify(prog, row):
    """The label of one valid line of PROG (a key of PROGRAMS), or None when it isn't classified."""
    entry = PROGRAMS.get(prog)
    if entry is None or entry == "BIN":
        return None
    if isinstance(entry, str):
        return entry
    key = subcommand(prog, row)
    if key not in entry:
        return None
    label = entry[key]
    return label(list(row)) if callable(label) else label


def classify_bin(cmd, args, remote=False):
    """The label of `metacomputer CMD ARGS...` (REMOTE: for a computer reached over SSH), or None when CMD isn't
    classified."""
    label = BIN.get(cmd)
    if not callable(label):
        return label
    import inspect
    return label(list(args), remote=remote) if "remote" in inspect.signature(label).parameters else label(list(args))
