"""Every program in the tree and its command lines, for tests/test_help.py (round 6).

A program is a tracked file that's executable, starts with #! or is Python with a __main__ (the suites, tests/test_*.py,
aside), or one the repository compiles (tools/*/main.go: scripts/build-tools.sh builds it). Each has an entry in PROGRAMS:
how it's run and its VALID command lines, as its callers pass them (each a caller's, or the words a person or
bin/metacomputer's dispatch hands on). Programs whose parser describes itself (lib/strictargs.py: argparse, or expect()'s
forms) get a valid line made from their declaration for every subcommand and form too.

Each valid line runs with MCOMP_PARSE_ONLY=1: the program's parser takes it and ends the run there, saying what that
form declares (its flags, its options that take a value, how many words it took and may take), with exit 99 ("parse
only: nothing was done"), never 0, so a guard run that way refuses. It must be taken, with nothing called, connected or
written: a program too strict for its own callers fails here (finalize.sh's two words).
Then every misuse is made from that declaration and put after the valid line (before a `--` whose rest is another
program's): -h and --help (answered or refused), -hx, --help=x, --hel, an option it doesn't have, each of its flags twice,
each option that takes a value twice, with --help and with an option (--x --bogus, --x=--bogus) for its value, and the
words it may take plus one; each is refused (exit 2) but the requests for usage, and nothing is called, connected or
written for any of them. The VM's programs are run with only their command-line guard (the lines between "metacomputer's
command-line rules" and "end of the command-line rules"): on this Mac their bodies would act on the wrong machine.
"""
import json
import os
import re
import subprocess

# Programs this scan doesn't find, and why they're outside tests/test_acting.py's sweep. The vault is compiled Swift
# (scripts/build-vault.sh), run from an installed copy, and walked form by form by tests/test_vault_ops.py; its own CLI
# run directly (`mcomp-vault fill ...`) isn't handoff-gated: `metacomputer secrets fill|login|session load` and the hub's
# fills are (bin/metacomputer handoff_hold). A program running as you can call the vault's CLI as it can ssh: the
# same-account limit (SECURITY.md).
OUTSIDE = {"vault/ (mcomp-vault)": "compiled Swift, run from an installed copy; its own CLI isn't handoff-gated, "
                                   "metacomputer's secrets commands and the hub's fills are (the same-account limit)"}

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
G, R, S = ("provisioning/guest/rootfs/opt/metacomputer/bin/", "provisioning/remote/rootfs/opt/metacomputer/bin/",
           "providers/sprites/guest/rootfs/opt/metacomputer/sprite/")
BOX = "{BOX}"
CO = "{CO}"


def P(run, rows, env=None, stdin="", synth=False, by="callers"):
    """RUN: py, bash, sh, guard (the VM's: its guard alone), go (built from tools/), uninstalled (a launcher with no
    metacomputer set up). ROWS: valid argv lists. SYNTH: also a valid line per declared form/subcommand. BY: who runs it
    (callers: some file in the repository does; person: a person, by hand, as its usage says)."""
    return {"run": run, "rows": rows, "env": env or {}, "stdin": stdin, "synth": synth, "by": by}


PROGRAMS = {
    "bin/metacomputer": "FORMS",                       # every command of its dispatch: tests/test_help.py FORMS
    "agent-plugin/bin/metacomputer": P("bash", [["status"], ["checkpoint", "--delete", "keep", "--yes"]], {"MCOMP_HOME": CO}),
    "agent-plugin/bin/metacomputer#uninstalled": P("uninstalled", [["--help"], ["-h"]]),
    "plugin/bin/metacomputer": P("bash", [["status"], ["checkpoint", "--delete", "keep", "--yes"]], {"MCOMP_HOME": CO}),
    "plugin/bin/metacomputer#setup": P("bash", [["setup", "--no-build"], ["setup", "--dir", "x", "--yes"]],
                                       {"CLAUDE_CODE_PLUGIN_CACHE_DIR": CO}),
    "plugin/bin/metacomputer#uninstalled": P("uninstalled", [["--help"], ["-h"]]),
    "get.sh": P("bash", [["--confirm", "--no-build"], ["--dir", "x", "--ref", "v1.0.0", "--yes"]]),
    "plugin/get.sh": P("bash", [["--confirm", "--no-build"]]),
    "install.sh": P("bash", [["--yes"], ["--no-build"], ["--lume=standard", "--no-claude"], ["--uninstall"]]),
    "scripts/build-tools.sh": P("bash", [[], ["--check"]]),
    "scripts/build-vault.sh": P("bash", [[], ["--force"], ["--test", "x"], ["--out", "x"]]),
    "scripts/build-vm.sh": P("bash", [[], ["--replace"]]),
    "scripts/check-public.sh": P("bash", [["-a"], ["-r", ".", "list.txt"]], by="person"),
    "scripts/fetch-artifacts.sh": P("bash", [[], ["rclone"], ["remote", "arm64"], ["remote", "x86_64"]]),
    "scripts/lib.sh": P("bash", [], by="sourced"),
    "scripts/vm-commands.sh": P("bash", [], by="sourced"),
    "scripts/pin-gpg-key.sh": P("bash", [["b", "0" * 40, "o"]]),
    "scripts/release.sh": P("bash", [["9.9.9"], ["9.9.9", "--dry-run", "--skip-tests"], ["9.9.9", "--tag", "--draft-release"]],
                            by="person"),
    "scripts/remote-build.sh": P("bash", [[], ["--keep-on-failure"]]),
    "scripts/safe-rmtree.sh": P("bash", [[BOX + "/victim"], ["--quiet", BOX + "/victim", BOX + "/victim2"]]),
    "scripts/sync-plugin.py": P("py", [[], ["--check"]], synth=True),
    "scripts/gen-providers.py": P("py", [[], ["--check"]], synth=True, by="person"),
    "scripts/gen-agents.py": P("py", [[], ["--check"]], synth=True, by="person"),
    "scripts/build-openai-plugin.py": P("py", [["--check"], ["--version", "1.2.3", "--out", "x.zip"]], synth=True),
    "scripts/vendor-driver-skill.py": P("py", [["src"]], synth=True, by="person"),
    "scripts/verify-audit.py": P("py", [["f.jsonl"], ["--key", "k", "--expect-start", "1", "f.jsonl"]], synth=True),
    "tests/agentic_claude.sh": P("bash", [[], ["task.md"]], by="person"),
    "tests/run_tests.py": P("py", [["A"], ["A", "B", "--run-id", "r1"]], synth=True),
    "tests/skill/analyze.py": P("py", [["t.jsonl"]], synth=True),
    "tests/skill/run_headless.sh": P("bash", [["o", "n", "w", "p"], ["o", "n", "w", "p", "--global-mcp"]]),
    "bench/bench.py": P("py", [["list"], ["run", "--tasks", "ff-form", "--run-id", "try-1"]], synth=True),
    "bench/guest/calc-overlay.sh": P("guard", [["lodeb"]]),
    "bench/guest/server.py": P("py", [["site", "requests.jsonl", "8000"], ["root", "log"]]),
    "lib/agentkit/recognition.py": P("py", [["swift"], ["check"]], synth=True),
    "lib/actionlog.py": P("py", [["list"], ["show", "latest", "--json"], ["verify", "--all"], ["verify", "f.jsonl"]]),
    "lib/agent_setup.py": P("py", [["link-ours", "p", "--root", "r"], ["install", "openclaw", "--root", "r", "--link", "l"],
                                   ["status", "antigravity", "--root", "r", "--link", "l"]], synth=True),
    "lib/agents.py": P("py", [["check", "--quiet"], ["status", "--json"]], synth=True),
    "coding-agents/codex/setup.py": P("py", [["codex-entry", "--root", "r", "--link", "l"]], synth=True),
    "lib/botcheck.py": P("py", [[]], stdin="{}", by="person"),
    "lib/browser_tools.py": P("py", [["read", "https://example.com"], ["page", "open", "https://example.com"]], synth=True),
    "coding-agents/claude-code/claude_mcp.py": P("py", [["user-get", "n", "--root", "r", "--link", "l"], ["user-recheck", "n"]], synth=True),
    "lib/defaultvm.py": P("py", [["read", "f"], ["read"]], synth=True),
    "lib/driver_profiles.py": P("py", [["render"], ["launch-path", "p"]], synth=True),
    "lib/each.py": P("py", [["main,other", "--", "status"], ["--timeout", "5", "main", "--", "reset", "--yes"]], synth=True),
    "lib/events.py": P("py", [["status"], ["needs-watcher"], ["watch"]], synth=True),
    "lib/guest_import.py": P("py", [["dest"], ["dest", "--quiet", "--timeout", "5", "--", "ssh", "-T", "h"]], synth=True),
    "lib/guest_inspect.py": P("py", [["system"], ["browser"], ["--render"], ["--json-out"], ["all", "--minutes", "5", "--top", "3"]]),
    "lib/guest_profiles.py": P("py", [["new", "firefox", "p"], ["check", "chrome", "p"], ["delete", "firefox", "p"]]),
    # (bin/metacomputer's put passes the ops SSH command, which names the computer's SSH folder: its known-hosts file)
    "lib/guest_put.py": P("py", [["--transport", "4", "ssh", "-o", "UserKnownHostsFile={BOX}/home/.ssh/metacomputer/known_hosts",
                                  "-T", "f"],
                                 ["--transport", "3", "ssh", "-o", "UserKnownHostsFile={BOX}/home/.ssh/metacomputer/known_hosts",
                                  "f", "g", "--to", "d", "--overwrite"]]),
    "lib/handoff.py": P("py", [["status", "--json"], ["done", "--note", "n"], ["cancel"], ["wait"], ["guard", "--hold-fd", "6"],
                               ["guard"], ["_dialog", "h1"], ["--why", "login", "--say", "- sign in, then Hand back", "--json"],
                               ["done", "--note", "-- signed in"],
                               # handoff_hold_unless_user (bin/metacomputer), script_handoff_hold (scripts/lib.sh), the
                               # hub's own input reset's ssh; the Hand back panel's helper (start_panel, view --open)
                               ["guard", "--hold-fd", "6", "--user-ok"], ["guard", "--hold-fd", "6", "--user-ok", "--hub-child"],
                               ["guard", "--hold-fd", "5", "--user-ok"], ["_panel", "h-0123456789ab"], ["_panel-open"]]),
    "lib/input_reset.py": P("py", [["view", "--transport", "2", "ssh", "-T"], ["reset", "--check", "--transport", "1", "ssh"]],
                            synth=True),
    "lib/learn.py": P("py", [["list", "--json"], ["report", "--since", "7d"]], synth=True),
    "lib/liveview.py": P("py", [["gui"], ["remembered", "--port", "5901"], ["remember", "--port", "5901", "--from-view"]],
                         synth=True),
    "lib/mcomp_sdk.py": P("py", [["probe"], ["tools"], ["describe", "list_windows"], ["call", "list_windows"],
                                 ["call", "--session", "s1", "click", "{}"],
                                 ["call", "--session", "s1", "--json", "type_text", "-"]], stdin="{}", synth=True),
    "lib/mcp_client.py": P("py", [["--tools"], ["--probe"], ["--describe", "list_windows"], ["--call", "list_windows", "{}"]],
                           synth=True),
    "lib/mcp_hub.py": P("py", [["stop", "--quiet"], ["status", "--json"], ["stop", "--if-idle"], ["serve"]], synth=True),
    "lib/mcp_lazy.py": P("py", [[], ["--refresh-cache", "--seed"]], synth=True),
    "lib/mcp_relay.py": P("py", [["ssh", "-T", "h", "req"]]),
    "lib/netpolicy.py": P("py", [["status", "--json"], ["allow", "x.com", "--for", "1h"], ["render"]], synth=True),
    "lib/owners.py": P("py", [["check"], ["who", "--json"], ["release"], ["at-terminal", "--stderr"],
                              # _vnc-install's and _vnc-push's checks (bin/metacomputer)
                              ["internal-caller", "--user", "script:build-vm.sh", "script:remote-build.sh"],
                              ["internal-caller", "--user"]], synth=True),
    "lib/page_guest.py": P("py", [["read", "{}"], ["page-close", "{}"]]),
    "lib/placement.py": P("py", [["resolve", "--vm", "v"], ["name-ok", "n"], ["places"]], synth=True),
    "lib/proxywrap.py": P("py", [["--", "/usr/bin/true"], ["--id", "sprite-1", "--check", "1", "/usr/bin/true", "--", "/usr/bin/true", "-x"]]),
    "lib/recording.py": P("py", [["probe", "f.mkv"], ["capture", "--max-bytes", "10", "--timeout", "5", "--", "ssh", "-T", "h"],
                                 # record_stream_start (bin/metacomputer): the stream's Mac half, keeping the lock
                                 ["save", "f.mkv", "--min-free-gb", "10", "--hold-fd", "6", "--", "ssh", "-T", "h"]],
                          synth=True),
    "lib/remote.py": P("py", [["missing", "--provider", "oci", "--feature", "x"], ["exec", "--vm", "demo", "--", "/usr/bin/true", "--help"],
                              ["plan", "--vm", "v"],
                              # the forms that change a computer, as bin/metacomputer and scripts/remote-build.sh pass
                              # them (tests/test_acting.py runs each while a handoff is open)
                              ["checkpoint", "--vm", "demo", "create", "keep", "--replace"],
                              ["checkpoint", "--vm", "demo", "restore", "keep"], ["checkpoint", "--vm", "demo", "delete", "keep"],
                              ["egress", "--vm", "demo", "--set", "off"], ["egress", "--vm", "demo", "--set", "internet", "--setup"],
                              ["sleep", "--vm", "demo", "--force"], ["delete", "--vm", "demo", "--network-too"],
                              ["abort", "--vm", "demo", "--op", "op1"], ["allow", "--vm", "demo", "--clear"],
                              ["cloud", "teardown", "hetzner"], ["cloud", "teardown", "hetzner", "--forget", "0" * 32],
                              ["cloud", "setup", "oci"]],
                       synth=True),
    "lib/safedelete.py": P("py", [["rmtree", BOX + "/victim"], ["rmtree", "--quiet", "--", BOX + "/victim"],
                                  ["check-runner", "--", "d"], ["prune", "--older-than", "3600", "--", "d"],
                                  ["own", "--base", "b", "--rel", "a/n", "--target-vm", "n", "--check"]], synth=True),
    "lib/sessionvm.py": P("py", [["guard", "--vm", "v", "--ssh-port", "2222", "--chosen", "main", "--settings", "", "--", "status"],
                                 ["show", "--json"]], synth=True),
    "lib/shop.py": P("py", [["status", "--json"]], synth=True),
    "lib/storage.py": P("py", [["volume", "list", "--json"], ["status"],
                               # the forms that change a computer, as bin/metacomputer and scripts/lib.sh pass them
                               ["volume", "sync", "v", "--force"], ["kill-vm", "--grace", "10"], ["release", "--quiet"],
                               ["checkpoint", "save", "ck"], ["checkpoint", "restore", "ck"], ["checkpoint", "delete", "ck"],
                               ["prepare", "--pid", "1", "--no-volumes"]], synth=True),
    "lib/termsafe.py": P("py", [[], ["--max", "65536"]], synth=True),
    "lib/traces.py": P("py", [["list"], ["show", "t", "--json"]], synth=True),
    "lib/tunnellog.py": P("py", [["--log", BOX + "/t.log", "--fork", "--", "ssh", "-f", "-N", "h"],
                                 ["--log", BOX + "/t.log", "--listen", "5901", "--wait", "15", "--", "ssh", "-N", "h"]], synth=True),
    "lib/update.py": P("py", [["version", "--json"], ["update", "--check"], ["_record-build", "d", ""], ["_guest-digest"],
                              ["_runinfo-json", "v", "running", "d", "n", "g", "h", "", "", ""], ["_refresh-check"],
                              ["_after-install"], ["_status-line"]], synth=True),
    "lib/vm_claude.py": P("py", [["--transport", "1", "ssh", "status"], ["--transport", "1", "ssh", "stop", "--revoke"],
                                 ["--transport", "1", "ssh", "send", "hi", "--wait"], ["--transport", "1", "ssh", "keys", "Enter"],
                                 ["--transport", "1", "ssh", "read", "--lines", "5"], ["--transport", "1", "ssh", "start", "--", "--model", "x"]]),
    "lib/vm_claude_guest.sh": P("guard", [["status", "s1"], ["stop", "s1"], ["probe", "s1"], ["read", "s1", "40"], ["show", "s1", "0"],
                                          ["wait", "s1", "600", "5", "40"], ["send", "s1", "1", "0", "5", "600", "40"],
                                          ["keys", "s1", "Enter"], ["start", "s1", "w", "api-key", "", "0", "0", "1", "--", "--help"]]),
    "lib/vm_shell.py": P("py", [["--transport", "1", "ssh", "-c", "true"], ["--transport", "1", "ssh", "--file", "f", "--json", "--", "a"],
                                ["--transport", "1", "ssh", "-", "--read-only"]]),
    "lib/vm_shell_guest.py": P("py", [[]], stdin=""),
    "lib/vms.py": P("py", [["all-known"], ["list", "--settings", "s", "--runner", "r"],
                           # scripts/vm-commands.sh's vm_list: what chose the VM, said as typed ("--vm NAME")
                           ["list", "--settings", "s", "--runner-failed", "--selected", "", "--default", "demo",
                            "--default-problem", "", "--overridden-by", "--vm demo", "--json"]], synth=True),
    "tools/qcow2raw": P("go", [["in.qcow2", "out.raw"], ["in.qcow2", "out.raw", "1000"]]),
    "provisioning/guest/finalize.sh": P("guard", [["cuaadmin", "cua"], ["cuaadmin"]]),
    "provisioning/guest/provision.sh": P("guard", [["/opt/metacomputer-payload"]]),
    "provisioning/remote/bootstrap.sh": P("guard", [["/run/metacomputer-bootstrap", "", "22"], ["/tmp/d", "owner", "2222"]]),
    "providers/sprites/guest/bootstrap.sh": P("guard", [["/tmp/d"]]),
    G + "desktop-record": P("guard", [["start", "n", "20", "standard", "off"], ["stream", "n", "20", "high", "auto"], ["stop"],
                                      ["status"]]),
    G + "driver-mcp": P("guard", [[]]),
    G + "guest-status": P("py", [[]]),
    G + "input-reset": P("guard", [[], ["--check"]]),
    G + "secret-fill": P("py", [[], ["--deadline", "5", "--proc-net", "a", "--proc-net", "b"]], stdin="{}"),
    G + "session-init": P("guard", [[]]),
    G + "session-run": P("guard", [["import", "-window", "root", "png:-"], ["sh", "-c", "x"]]),
    G + "storage-helper": P("py", [["status"], ["version"], ["attach"]]),
    "provisioning/guest/rootfs/usr/local/sbin/metacomputer-admin": P("guard", [
        ["poweroff"], ["restart-desktop"], ["status"], ["logs", "400"], ["provision-log"], ["updates"], ["firewall"],
        ["egress", "status"], ["egress", "off"], ["idle"], ["idle", "15"], ["ssh-auth", "cert"], ["driver-policy", "bounded"],
        ["storage", "attach"], ["reboot"]]),
    R + "guest-firewall": P("guard", [["apply"], ["mark-setup"], ["end-setup"], ["status"], ["render-emergency", "--ports", "22"],
                                      ["render", "--egress", "off", "--ports", "22", "--uids", "1"]]),
    R + "idle-stop": P("guard", [[], ["check"], ["status"]]),
    R + "remote-uninstall": P("guard", [["rollback"], ["all"]]),
    S + "hide-sprite-api": P("guard", [["/usr/bin/tini", "--", "/usr/bin/sleep", "infinity"]]),
    S + "in-session": P("guard", [["runuser", "-u", "cua", "--", "env", "-i"], ["/usr/sbin/sshd", "-D", "-e"]]),
    S + "services": P("guard", [["sshd"], ["desktop"], ["status"]]),
    S + "svc-desktop": P("guard", [[]]),
    S + "svc-session": P("guard", [[]]),
    S + "svc-sshd": P("guard", [[]]),
    S + "svc-xvfb": P("guard", [[]]),
    S + "xvfb-inner": P("guard", [[]]),
}

# The VM's guards' own options (by program, or by program and its first word), as their guard lines say (checked: each
# is in its guard), and the programs whose words are all another program's.
GUARD_OPTIONS = {G + "input-reset": {"flags": ["--check"], "values": []},
                 (R + "guest-firewall", "render"): {"flags": [], "values": ["--egress", "--ports", "--uids", "--until"]},
                 (R + "guest-firewall", "render-emergency"): {"flags": [], "values": ["--ports", "--uids"]}}
PASSTHROUGH = {G + "session-run", S + "hide-sprite-api", S + "in-session"}      # CMD [ARGS...]: the rest is CMD's
# Guards whose words have no fixed most (the action's own words follow): keys and start in vm_claude_guest.sh.
GUARD_ANY_WORDS = {("lib/vm_claude_guest.sh", "keys"), ("lib/vm_claude_guest.sh", "start")}


MAIN = re.compile(rb"""^if __name__ == ["']__main__["']:""", re.M)     # a Python program's own entry, at a line's start


def entry_points():
    """Every program, by a scan (not a list): tracked files that are executable, start with #! or are Python with a
    __main__ (the suites aside), and the Go programs the repository compiles (tools/NAME/main.go)."""
    out = subprocess.run(["git", "-C", LAB, "ls-files", "-s"], capture_output=True, text=True).stdout.splitlines()
    found = set()
    for line in out:
        meta, path = line.split("\t", 1)
        full = os.path.join(LAB, path)
        if (path.startswith("tests/test_") and path.endswith(".py")) or not os.path.isfile(full) or os.path.islink(full):
            continue
        data = open(full, "rb").read()
        if path.startswith("tools/") and path.endswith("/main.go") and b"package main" in data:
            found.add(os.path.dirname(path))
            continue
        if meta.split()[0] == "100755" or data.startswith(b"#!") or (path.endswith(".py") and MAIN.search(data)):
            found.add(path)
    return found


def guard_of(rel):
    """The VM program's command-line guard: the lines between its "metacomputer's command-line rules" note and "end of
    the command-line rules"."""
    lines = open(os.path.join(LAB, rel)).read().split("\n")
    i = next(n for n, ln in enumerate(lines) if ln.startswith("# metacomputer's command-line rules"))
    j = next(n for n in range(i, len(lines)) if lines[n].startswith("# end of the command-line rules"))
    return "\n".join(ln for ln in lines[i:j] if not ln.startswith("#"))


def caller_lines(rel):
    """The lines of the repository that run REL (by its file name), outside REL itself and comments."""
    base = os.path.basename(rel)
    out = subprocess.run(["git", "-C", LAB, "grep", "-n", "-F", base, "--", "bin", "scripts", "lib", "provisioning",
                          "providers", "coding-agents", "vault", "install.sh", "get.sh", "plugin/bin", "agent-plugin/bin", "tests/run_tests.py", "bench",
                          "gemini-extension.json"], capture_output=True, text=True).stdout.splitlines()
    return [ln for ln in out if not ln.startswith(rel + ":") and not re.match(r"^[^:]+:\d+:\s*#", ln)]


def synth_rows(desc):
    """A valid command line for each form (expect) or each subcommand (argparse) a program's declaration describes."""
    if "forms" in desc:
        return [synth_form(f) for f in desc["forms"]]
    rows = []

    def sample(e):
        if e.get("choices"):
            return str(e["choices"][0])
        return "1" if e.get("int") else "x"

    def walk(t, pre):
        if t["subs"]:
            for name, sub in t["subs"].items():
                walk(sub, pre + [name])
            return
        row = list(pre)
        for o in t["options"]:
            if o.get("required"):
                row += [o["opt"]] + ([] if o["nargs"] == 0 else [sample(o)])
        for grp in t.get("groups", []):
            row += grp                          # a required group's first option (and a value, if it takes one)
        tail = []
        for p in t["positionals"]:
            n = p["nargs"]
            if n == "A...":                     # argparse.REMAINDER: another program's words
                tail += ["--", "/usr/bin/true"]
            elif n in (None, "+") or isinstance(n, int):
                row += [sample(p)] * (n if isinstance(n, int) else 1)
        rows.append(row + tail)
    walk(desc, [])
    return rows


def synth_form(f):
    """The words a form needs (what's in [...] left out): --name and its VALUE, each word, `--` and a program after it."""
    row, depth = [], 0
    for t in f.split():
        inside = depth > 0 or t.startswith("[")
        depth += t.count("[") - t.count("]")
        if inside:
            continue                            # (only what the form needs)
        body = t[:-3] if t.endswith("...") else t
        if body == "--" or body.startswith("--"):
            row.append(body)
        elif body.startswith("~"):
            row.append("/usr/bin/true" if row[-1:] == ["--"] else "x")
        elif body[:1].isupper():
            row.append("1" if body in ("N", "PORT", "SECONDS", "BYTES") else "x")
        else:
            row.append(body.split("|")[0])
    return row


def misuse_rows(row, info):
    """Every misuse of the form ROW is, made from what its parser said it declares (INFO), put after it (before a `--`
    whose rest is another program's; at the start for a program that passes its words on)."""
    if info.get("passthrough"):
        at = 0                                  # CMD [ARGS...]: all of it is CMD's
    else:
        at = row.index("--") if "--" in row else len(row)

    def put(*words):
        return row[:at] + list(words) + row[at:]
    out = [("a request for usage (-h)", put("-h"), "either"), ("a request for usage (--help)", put("--help"), "either")]
    for bad in ("-hx", "--help=x", "--hel"):
        out.append((f"something shaped like a request for usage ({bad})", put(bad), "refuse"))
    out.append(("an option it doesn't have", put("--no-such-option"), "refuse"))
    for f in info.get("flags") or []:
        out.append((f"its flag {f} twice", put(*([f] if f in row else [f, f])), "refuse"))
    for v in info.get("values") or []:
        if v not in (info.get("repeatable") or []):         # (an option meant to repeat: --logs, --own, --proc-net)
            out.append((f"its option {v} twice", put(v, "x", v, "y"), "refuse"))
        out.append((f"a request for usage as {v}'s value", put(v, "--help"), "either"))
        if v in (info.get("free") or []):       # ~VALUE: any word but a request for usage (sessionvm's settings label)
            continue
        out.append((f"an option as {v}'s value", put(v, "--bogus"), "refuse"))
        if v.startswith("--"):
            out.append((f"an option as {v}'s value ({v}=--bogus)", put(f"{v}=--bogus"), "refuse"))
    most, used = info.get("max_words"), info.get("words") or 0
    if most is not None and not info.get("passthrough"):
        out.append(("a word too many", put(*[f"extra{k}" for k in range(max(1, most - used + 1))]), "refuse"))
    return out


def guard_info(rel, row):
    """What a VM program's guard takes for ROW: its options (GUARD_OPTIONS), and whether a word more is refused."""
    info = dict(GUARD_OPTIONS.get((rel, row[0] if row else ""), GUARD_OPTIONS.get(rel, {"flags": [], "values": []})))
    if rel in PASSTHROUGH:
        info["passthrough"] = True
    elif (rel, row[0] if row else "") in GUARD_ANY_WORDS:
        info["max_words"] = None
    else:
        info["words"], info["max_words"] = len([a for a in row if not a.startswith("-")]), len(
            [a for a in row if not a.startswith("-")])
    return info
