#!/usr/bin/env python3
"""--help and -h, wherever a person types them: the usage, exit 0, and nothing else.

Every command of bin/metacomputer and its subcommands (and the programs it hands them to: scripts/*.sh, lib/*.py) is
run with --help and with -h, after the command and after its subcommand and arguments, on a copy of the checkout
(tests/test_multi_vm.py's Box) with a throwaway HOME. Stand-ins on PATH record any call of the programs that reach a
VM, the VM runner, a provider, the vault, the network or this Mac's settings (ssh, the runner, sprite, oci, curl,
osascript, security, ...), and a sitecustomize.py on PYTHONPATH records every socket a Python helper opens and every
such program it starts by its full path. A form fails when it doesn't exit 0 with usage on stdout, when anything was
called, or when any file or folder of the copy, its HOME, its private folders or its socket folder was made, changed
or removed. A command added to the dispatch without rows here fails too. Every program in scripts/, install.sh,
get.sh and every lib/ program with a __main__, run on its own with -h and --help, must call and write nothing too; those
a person runs print their usage and exit 0.

No VM, no network; nothing of this Mac's VMs, settings, vault or agents is read or touched.

    python3 tests/test_help.py
"""
import concurrent.futures
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
from test_multi_vm import Box, PY, write  # noqa: E402

FAILS = []
COUNT = [0]
FLAGS = ("--help", "-h")
H = "{H}"

# Programs whose call is an action: the VM, its runner, the network, a provider, the vault, this Mac's settings.
STANDINS = ("ssh", "scp", "sftp", "mosh", "mosh-client", "rsync", "curl", "wget", "git", "nc", "ncat", "socat",
            "osascript", "security", "open", "pbcopy", "pbpaste", "launchctl", "tailscale", "sprite", "oci", "sudo",
            "ssh-keygen", "ssh-add", "ssh-agent", "claude", "codex", "gemini", "openclaw", "hermes", "npm", "npx",
            "node", "brew", "swift", "swiftc", "xcrun", "xcodebuild", "make", "go", "cargo", "cc", "clang",
            "sandbox-exec", "pfctl", "ffmpeg", "ffprobe", "rclone", "hdiutil", "diskutil", "qemu-img", "mkisofs",
            "xorriso", "lume", "mcomp-vm", "lume-secure", "caffeinate", "pkill", "killall", "shop", "say", "afplay",
            "tmux", "screencapture", "defaults", "networksetup", "softwareupdate", "gh", "mcomp-vault",
            "tccutil", "dscl", "sysadminctl", "pmset", "scutil", "vncpasswd", "tigervncpasswd", "uv", "pip", "pip3")

RECORDER = """#!/bin/sh
printf '%s\\n' "standin $(basename "$0") $*" >> "${MCOMP_HELP_CALLS:-/dev/null}"
exit 97
"""
# The VM runner: every call recorded; it answers where it keeps VMs and that it has none (what loading a computer's
# settings asks of it, scripts/lib.sh's identity check), so the copy's plain commands run (warm).
RUNNER = """#!/bin/sh
printf '%s\\n' "standin $(basename "$0") $*" >> "${MCOMP_HELP_CALLS:-/dev/null}"
case "$1 ${2:-}" in
  "config get") printf 'Default VM storage: home (%s)\\n\\nConfigured VM storage locations:\\n  - home: %s (default)\\n' \
                  "$FAKE_STORAGE" "$FAKE_STORAGE" ;;
  "ls "*) echo '[]' ;;
  *) exit 97 ;;
esac
"""

SITECUSTOMIZE = r'''
import os
import socket
import subprocess

_LOG = os.environ.get("MCOMP_HELP_CALLS")
_NAMES = set((os.environ.get("MCOMP_HELP_WATCH") or "").split(","))


def _note(what):
    try:
        fd = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (" ".join(str(what).split())[:400] + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        pass


if _LOG:
    def _connect(self, addr):
        _note(f"python connect {self.family!r} {addr!r}")
        raise OSError(111, "connections are refused while --help runs (tests/test_help.py)")

    def _connect_ex(self, addr):
        _note(f"python connect_ex {self.family!r} {addr!r}")
        return 111

    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    _init = subprocess.Popen.__init__

    def _popen(self, args, *a, **k):
        exe = k.get("executable") or (args if isinstance(args, (str, bytes)) else (list(args) or [""])[0])
        if os.path.basename(os.fsdecode(exe)).split()[0:1] and os.path.basename(os.fsdecode(exe)).split()[0] in _NAMES:
            _note(f"python runs {os.fsdecode(exe)}")
        return _init(self, args, *a, **k)

    subprocess.Popen.__init__ = _popen
'''

# The settings of the copy's computers: its main VM, another VM on this Mac, your own server and a Fly Sprite.
VMS = {
    "config/local.env": 'VM_NAME=helpvm\nBASELINE_NAME=helpvm-baseline\nBROWSERS="firefox"\n',
    "config/vms/other.env": "SSH_PORT=2402\nVNC_PORT=6102\n",
    "config/vms/far.env": ("SSH_PORT=2403\nVNC_PORT=6103\nPLACE=server\nPROVIDER=ubuntu-vm\nTRANSPORT=ssh\n"
                           "SSH_HOST=192.0.2.10\nSSH_HOST_PORT=2222\nSETUP_USER=owner\nIDLE_STOP=off\nEGRESS=internet\n"),
    "config/vms/spr.env": ("SSH_PORT=2404\nVNC_PORT=6104\nPLACE=sprites\nPROVIDER=sprites\nTRANSPORT=sprite\n"
                           "SPRITE_ORG=my-org\nSPRITE_NAME=sp1\nEGRESS=internet\n"),
}
URL = "https://example.com"

# (computer: [] for the default one, or ["--vm", NAME]; the command; its arguments, {H} where the flag goes).
# Destructive commands first; then every other command and subcommand of the dispatch.
FORMS = [
    # build, reset, restore, baseline: the VM's disk
    ([], "build", [H]), ([], "build", ["--replace", H]), ([], "build", [H, "--replace"]),
    (["--vm", "other"], "build", [H]), (["--vm", "other"], "build", ["--replace", H]),
    ([], "reset", [H]), ([], "reset", ["--yes", H]), ([], "reset", ["--yes", "--discard-unsynced", H]),
    (["--vm", "other"], "reset", ["--yes", H]),
    ([], "restore", [H]), ([], "restore", ["ck", H]), ([], "restore", ["ck", "--yes", H]), ([], "restore", [H, "ck"]),
    ([], "baseline", [H]), ([], "baseline", ["--yes", H]), (["--vm", "other"], "baseline", ["--yes", H]),
    ([], "checkpoint", [H]), ([], "checkpoint", ["ck", H]), ([], "checkpoint", ["ck", "--replace", H]),
    ([], "checkpoint", ["--delete", "ck", H]), ([], "checkpoint", ["--delete", "ck", "--yes", H]),
    ([], "checkpoints", [H]),
    # vm create / delete, cloud teardown
    ([], "vm", [H]), ([], "vm", ["create", H]), ([], "vm", ["create", "newvm", H]),
    ([], "vm", ["create", "newvm", "--yes", H]), ([], "vm", ["create", "newvm", "--no-build", "--yes", H]),
    ([], "vm", ["create", "newvm", "--on", "oci", "--yes", H]),
    ([], "vm", ["create", "newvm", "--on", "server", "--host", "192.0.2.9", "--user", "u", H]),
    ([], "vm", ["create", "newvm", "--on", "sprites", "--org", "o", H]),
    ([], "vm", ["delete", H]), ([], "vm", ["delete", "other", H]), ([], "vm", ["delete", "other", "--confirm", "other", H]),
    ([], "vm", ["delete", "other", "--confirm", "other", "--force", "--delete-vault", "--delete-volumes", H]),
    ([], "vm", ["delete", H, "other", "--confirm", "other"]), ([], "vm", ["list", H]),
    ([], "cloud", [H]), ([], "cloud", ["teardown", H]), ([], "cloud", ["teardown", "oci", H]),
    ([], "cloud", ["teardown", "sprites", H]), ([], "cloud", ["teardown", "hetzner", "--forget", "0" * 32, H]),
    ([], "cloud", ["status", "oci", H]), ([], "cloud", ["doctor", "vultr", H]), ([], "cloud", ["setup", "oci", H]),
    ([], "cloud", ["setup", "digitalocean", "--json", H]),
    # secrets (the vault): every subcommand
    ([], "secrets", [H]),
    *[([], "secrets", [*sub, H]) for sub in (
        ["init"], ["set", "x"], ["set", "x", "--kind", "card"], ["delete", "x"], ["bind"], ["policy", "x"],
        ["policy", "x", "--approval", "each"], ["unlock"], ["unlock", "--for", "30m"], ["rekey"], ["recover"],
        ["rotate-session-key"], ["approve", "abcd1234"], ["rule", "add"], ["rule", "list"], ["rule", "remove", "x"],
        ["import-passwords", "--browser", "chrome"], ["import-session", "--browser", "chrome"],
        ["audit-sink", "add"], ["audit-sink", "remove", "x"], ["audit-sink", "list"], ["list"],
        ["fill", "x", "--field", "password"], ["fill", "x", "--field", "otp", "--human"], ["status"], ["audit"],
        ["login", "x", "--password-selector", "#p"], ["requests"], ["deny", "abcd1234"], ["lock"],
        ["session", "save", "x", "--profile", "p"], ["session", "load", "x"], ["session", "policy", "x"],
        ["session", "delete", "x"], ["token", "x"], ["token", "renew", "--all"], ["tokens"], ["revoke", "--all"],
        ["ask", "--site", "s", "--kind", "code", "--why", "w"], ["give", "abcd1234"], ["code", "--site", "s"],
        ["relay", "--site", "s"], ["pay", "card", "--origin", URL], ["pay", "--purchase", "k", "--done"],
        ["merchant-rules", URL], ["ap2", "key"], ["ap2", "init"], ["wba", "status"], ["wba", "init"],
        ["shop-card", "x", "--spend-request", "y"], ["shop-complete", "c", "--shop", "s"], ["shop-cli", "set"],
        ["spend-resolve"], ["disconnect"], ["version"])],
    ([], "secrets", [H, "set", "x"]),
    ([], "shop", [H]), ([], "shop", ["status", H]), ([], "shop", ["login", H]), ([], "shop", ["login", "--wait", "90", H]),
    ([], "shop", ["complete", "c", "--shop", "s", "--instrument", "-", H]), ([], "shop", ["card", "x", H]),
    # net (the VM's network)
    ([], "net", [H]), ([], "net", ["status", H]), ([], "net", ["allow", "example.com", H]),
    ([], "net", ["allow", "example.com", "--for", "2h", H]), ([], "net", ["remove", "example.com", H]),
    ([], "net", ["clear", H]), ([], "net", ["egress", "off", H]), ([], "net", [H, "clear"]),
    # the VM's life
    ([], "start", [H]), ([], "start", ["--no-volumes", H]), ([], "stop", [H]), ([], "stop", ["--force", H]),
    ([], "stop", ["--force", "--override", H]), ([], "restart", [H]), ([], "restart", ["--no-volumes", H]),
    ([], "status", [H]), ([], "status", ["--json", H]), ([], "desktop-restart", [H]),
    ([], "input-reset", [H]), ([], "input-reset", ["--check", H]), ([], "setup", [H]), ([], "setup", ["--yes", H]),
    ([], "setup", ["--no-build", "--yes", H]), ([], "update", [H]), ([], "update", ["--check", H]),
    ([], "update", ["--restart", H]), ([], "version", [H]), ([], "version", ["--json", H]),
    # computers elsewhere: their own start, stop, build, snapshots, network and live view
    *[(["--vm", "far"], c, a) for c, a in (
        ("start", [H]), ("stop", ["--force", H]), ("restart", [H]), ("build", [H]), ("build", ["--replace", H]),
        ("reset", ["--yes", H]), ("baseline", [H]), ("restore", ["ck", H]), ("checkpoint", ["ck", H]),
        ("checkpoints", [H]), ("net", ["egress", "off", H]), ("net", ["status", H]), ("net", ["allow", "x", H]),
        ("volume", ["list", H]), ("logs", [H]), ("view", ["--tunnel", H]), ("status", [H]), ("vm", ["delete", "far", H]))],
    *[(["--vm", "spr"], c, a) for c, a in (
        ("reset", ["--yes", H]), ("restore", ["ck", H]), ("checkpoint", ["ck", H]), ("checkpoint", ["--delete", "ck", H]),
        ("baseline", [H]), ("checkpoints", [H]), ("net", ["allow", "x", "--exact", H]), ("net", ["clear", H]),
        ("view", ["--tunnel", H]), ("logs", ["40", H]), ("desktop-restart", [H]), ("start", [H]), ("stop", [H]))],
    # the rest of the dispatch
    ([], "test", [H]), ([], "test", ["A", "B", H]), ([], "who", [H]), ([], "who", ["--json", H]), ([], "release", [H]),
    ([], "notify", [H]), ([], "notify", ["text", H]), ([], "notify", ["text", "--why", "login", "--action", "a", H]),
    ([], "handoff", [H]), ([], "handoff", ["--why", "login", "--say", "do it", H]), ([], "handoff", ["done", H]),
    ([], "handoff", ["done", "--note", "n", H]), ([], "handoff", ["cancel", H]), ([], "handoff", ["wait", H]),
    ([], "handoff", ["status", H]), ([], "handoff", ["status", "--json", H]),
    ([], "events", [H]), ([], "events", ["status", H]), ([], "events", ["sink", H]), ([], "events", ["sink", "add", H]),
    ([], "events", ["sink", "add", "x", "--url", URL, H]), ([], "events", ["sink", "list", H]),
    ([], "events", ["sink", "show", "x", H]), ([], "events", ["sink", "test", "x", H]),
    ([], "events", ["sink", "remove", "x", H]), ([], "events", ["tail", H]), ([], "events", ["tail", "5", H]),
    ([], "events", ["emit", "needs_you", "x", "--message", "m", H]), ([], "events", ["drain", H]),
    ([], "events", ["watcher", H]), ([], "events", ["watcher", "start", H]), ([], "events", ["watcher", "stop", H]),
    ([], "events", ["watcher", "status", H]),
    ([], "inspect", [H]), ([], "inspect", ["windows", H]), ([], "inspect", ["processes", "--json", H]),
    ([], "inspect", ["ports", H]), ([], "inspect", ["files", "--minutes", "5", H]), ([], "inspect", ["browser", H]),
    ([], "inspect", ["system", H]), ([], "where", [H]),
    ([], "call", [H]), ([], "call", ["list_windows", H]), ([], "call", ["--session", "s", "click", "{}", H]),
    ([], "hub", [H]), ([], "hub", ["status", H]), ([], "hub", ["status", "--json", H]), ([], "hub", ["start", H]),
    ([], "hub", ["stop", H]), ([], "hub", ["stop", "--if-idle", H]), ([], "hub", ["env", "--json", H]),
    ([], "browser", [H]), ([], "browser", ["default", H]), ([], "browser", ["default", "chrome", H]),
    ([], "browser", ["mode", "page", H]), ([], "browser", ["pacing", "fast", H]), ([], "browser", ["profiles", H]),
    ([], "browser", ["profiles", "--new", "p", H]), ([], "browser", ["profiles", "--delete", "p", "--yes", H]),
    ([], "browser", ["--json", H]),
    ([], "rename", [H]), ([], "rename", ["a", "b", H]),
    ([], "web", [H]), *[([], "web", [w, URL, H]) for w in ("compare", "responsive", "a11y", "open-untrusted", "read",
                                                             "page")],
    ([], "web", ["gpu", H]), ([], "web", ["debug", H]),
    ([], "trace", [H]), ([], "trace", ["start", "t", H]), ([], "trace", ["stop", H]), ([], "trace", ["list", H]),
    ([], "trace", ["show", "t", H]), ([], "trace", ["export", "t", "--out", "f", H]),
    ([], "each", [H]), ([], "each", ["main,other", H]), ([], "each", ["--timeout", "5", "main", H]),
    ([], "put", [H]), ([], "put", ["f", H]), ([], "put", ["f", "--to", "d", "--overwrite", H]),
    ([], "get", [H]), ([], "get", ["x", H]), ([], "get", ["x", "d", "--task", "t", H]),
    ([], "shot", [H]), ([], "shot", ["out.png", H]), ([], "contact-sheet", [H]), ([], "contact-sheet", ["x.mp4", H]),
    ([], "task", [H]), ([], "task", ["start", "slug", H]), ([], "task", ["start", "slug", "--no-sync", H]),
    ([], "task", ["finish", "mcomp-20260101-000000-slug", H]),
    ([], "list", [H]), ([], "list", ["--json", H]), ([], "list", ["--on", "mac", H]),
    ([], "default", [H]), ([], "default", ["other", H]), ([], "default", ["--json", H]),
    ([], "volume", [H]), ([], "volume", ["create", "v", "--size", "1G", H]), ([], "volume", ["create", "v", H]),
    ([], "volume", ["create", "v", "--folder", "/tmp", H]),
    ([], "volume", ["create", "v", "--s3", "--bucket", "b", "--endpoint", URL, H]), ([], "volume", ["list", H]),
    ([], "volume", ["delete", "v", H]), ([], "volume", ["delete", "v", "--yes", H]),
    ([], "volume", ["resize", "v", "--size", "2G", H]), ([], "volume", ["keys", "v", H]),
    ([], "volume", ["test", "v", H]), ([], "volume", ["sync", "v", "--force", H]),
    ([], "refdisk", [H]), ([], "refdisk", ["create", "r", "--from", "/tmp", H]), ([], "refdisk", ["list", H]),
    ([], "refdisk", ["delete", "r", "--yes", H]),
    ([], "record", [H]), ([], "record", ["start", H]), ([], "record", ["start", "n", "10", H]),
    ([], "record", ["stop", H]), ([], "record", ["status", H]), ([], "record", ["moments", "f.json", H]),
    ([], "recordings", [H]), ([], "recordings", ["--sync", H]), ([], "recordings", ["5", H]),
    ([], "view", [H]), *[([], "view", [v, H]) for v in ("--open", "--print", "--tailnet", "--forget", "--remember",
                                                          "--no-remember", "--tunnel")],
    ([], "view", ["--open", "--remember", H]),
    ([], "sh", [H]), ([], "sh", ["-c", "true", H]), ([], "sh", ["--file", "x.sh", H]), ([], "sh", ["-", H]),
    ([], "sh", ["-c", "true", "--read-only", "--json", H]), ([], "sh", [H, "-c", "true"]),
    ([], "claude", [H]), *[([], "claude", [*a, H]) for a in (
        ["start"], ["--token", "k"], ["start", "--token", "k", "--show"], ["send", "hi"], ["send", "hi", "--wait"],
        ["read"], ["read", "--lines", "5"], ["wait"], ["keys", "Enter"], ["show"], ["status"], ["renew"],
        ["renew", "--ttl", "1h"], ["stop"], ["stop", "--revoke"])],
    ([], "ssh", [H]), ([], "ssh", ["ops", H]), ([], "ssh", ["admin", H]),
    ([], "mcp", [H]), ([], "mcp-lazy", [H]),
    ([], "actions", [H]), ([], "actions", ["list", H]), ([], "actions", ["list", "--days", "3", H]),
    ([], "actions", ["show", H]), ([], "actions", ["show", "latest", "--json", H]), ([], "actions", ["verify", H]),
    ([], "actions", ["verify", "--all", H]),
    ([], "security", [H]), ([], "security", ["apply", H]), ([], "security", ["--json", H]),
    ([], "learn", [H]), *[([], "learn", [*a, H]) for a in (
        ["report"], ["report", "--since", "7d", "--json"], ["review"], ["list"], ["approve", "x"], ["reject", "x"],
        ["remove", "x"], ["edit", "x"], ["add"], ["bench", "--config", "t.env"])],
    ([], "agents", [H]), ([], "agents", ["--json", H]), ([], "agents", ["check", H]), ([], "agents", ["check", "--json", H]),
    ([], "logs", [H]), ([], "logs", ["40", H]),
    ([], "help", [H]), ([], "-h", []), ([], "--help", []), ([], "help", []),
    # metacomputer's own plumbing: never typed by a person, and still nothing on --help
    ([], "_health", [H]), ([], "_runinfo", [H]), ([], "_record-pipe", [H]), ([], "_task-sync", [H]),
    ([], "_vnc-install", [H]), ([], "_vnc-push", [H]),
]

# Where --help or -h is something else: a word of the guest's command, or a script's own argument after --. These
# run nothing either here (the stand-ins answer for the VM), and are never taken for metacomputer's usage.
NOT_HELP = [
    ([], "ssh", ["ops", "ls", H]),           # ls -h / ls --help in the guest
    ([], "sh", ["-c", "true", "--", H]),     # the script's own argument
]


# Programs run on their own (a developer's scripts, and the helpers bin/metacomputer runs): -h and --help start
# nothing, write nothing and end. Those a person runs print their usage and exit 0; the rest are metacomputer's own
# plumbing, which may answer with a usage error (exit 2) as long as they do nothing else.
SOURCED = {"scripts/lib.sh", "scripts/vm-commands.sh"}
PERSON_RUNS = {"install.sh", "get.sh", "lib/mcp_client.py"}      # besides every program in scripts/


def direct_targets():
    out = ["install.sh", "get.sh"]
    for d in ("scripts", "lib", "lib/cloud"):
        for f in sorted(os.listdir(os.path.join(LAB, d))):
            rel = f"{d}/{f}"
            if rel in SOURCED or not f.endswith((".sh", ".py")):
                continue
            if f.endswith(".py") and "__main__" not in open(os.path.join(LAB, rel)).read():
                continue
            out.append(rel)
    return out


def forms():
    out = []
    for vm, cmd, args in FORMS:
        if H not in args:
            out.append((vm, cmd, list(args)))
            continue
        for f in FLAGS:
            out.append((vm, cmd, [f if a == H else a for a in args]))
    return out


def dispatch_commands():
    """The command words of bin/metacomputer's main dispatch (its last `case "$cmd" in`), as test_session_guard reads them."""
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    block = src[src.rindex('case "$cmd" in'):]
    block = block[:block.index("\nesac")]
    words = set()
    for line in block.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and ")" in s and line.startswith("  ") and not line.startswith("   "):
            label = s.split(")", 1)[0]
            if label != "*":
                words.update(label.split("|"))
    return words


def check(name, cond, detail=""):
    COUNT[0] += 1
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-900:]})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Lab:
    """One copy of the checkout with its settings, stand-ins and watched folders (one per worker)."""

    def __init__(self, td, tag):
        self.box = Box(td, tag)
        shutil.copy(os.path.join(LAB, "get.sh"), self.box.co)
        self.calls_dir = os.path.join(td, "calls-" + tag)
        os.makedirs(self.calls_dir)
        self.standins = os.path.join(td, "standins-" + tag)
        for n in STANDINS:
            write(os.path.join(self.standins, n), RECORDER, 0o755)
        write(os.path.join(self.box.home, ".local", "bin", "mcomp-vm"), RUNNER, 0o755)          # the VM runner
        write(os.path.join(self.box.home, ".local", "share", "metacomputer", "bin", "mcomp-vault"), RECORDER, 0o755)
        write(self.box.vault, RECORDER, 0o755)                                                    # MCOMP_VAULT_BIN
        self.site = os.path.join(td, "site-" + tag)
        write(os.path.join(self.site, "sitecustomize.py"), SITECUSTOMIZE)
        for path, text in VMS.items():
            self.box.settings(path, text)
        self.n = 0

    def env(self, calls):
        e = self.box.env(MCOMP_HELP_CALLS=calls, MCOMP_HELP_WATCH=",".join(STANDINS), PYTHONPATH=self.site,
                         LUME=os.path.join(self.box.home, ".local", "bin", "mcomp-vm"),
                         HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                         ALL_PROXY="http://127.0.0.1:9", NO_PROXY="")
        e["PATH"] = self.standins + ":" + e["PATH"]
        for k in ("CODEX_SANDBOX", "CLAUDECODE", "SSH_AUTH_SOCK"):
            e.pop(k, None)
        return e

    def watched(self):
        """Every folder whose contents a command could change: the copy and its HOME, its private folders, the per-VM
        vaults' folder and the socket folders of its computers."""
        dirs = [self.box.root, self.box.tmp]
        uid = os.getuid()
        for vm in ("helpvm", "other", "far", "spr", "newvm", "main"):
            for co in {self.box.co, os.path.realpath(self.box.co)}:
                h = hashlib.sha1(f"{co}:{vm}".encode()).hexdigest()[:8]
                dirs.append(f"/tmp/mcomp-{uid}-{h}")
        return dirs

    def snapshot(self):
        seen = {}
        for d in self.watched():
            if not os.path.lexists(d):
                continue
            for root, dnames, fnames in os.walk(d):
                for n in dnames + fnames:
                    p = os.path.join(root, n)
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    seen[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
            try:
                st = os.lstat(d)
                seen[d] = (st.st_mode, st.st_size, st.st_mtime_ns)
            except OSError:
                pass
        return seen

    def run(self, vm, cmd, args, timeout=60):
        self.n += 1
        calls = os.path.join(self.calls_dir, f"{self.n}.log")
        before = self.snapshot()
        try:
            r = subprocess.run([self.box.mc, *vm, cmd, *args], env=self.env(calls), capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            rc, out, err = "timeout", (e.stdout or b"").decode("utf-8", "replace"), (e.stderr or b"").decode("utf-8", "replace")
        after = self.snapshot()
        called = open(calls).read().splitlines() if os.path.exists(calls) else []
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        return rc, out, err, called, changed

    def close(self):
        self.box.close()


def warm(lab):
    """What every command does before it reads its arguments (scripts/lib.sh makes the computer's run, log and socket
    folders), once per computer, so the forms are compared with that."""
    for vm in ([], ["--vm", "other"], ["--vm", "far"], ["--vm", "spr"]):
        rc, out, err, called, _ = lab.run(vm, "where", [])
        if rc != 0 or [c for c in called if not c.startswith("standin mcomp-vm config get")]:
            return f"`metacomputer {' '.join(vm)} where` on the copy: exit {rc}, calls {called}: {err[-600:]}"
    return None


def run_direct(lab, rel, flag, timeout=60):
    """One program run on its own with -h or --help, from the copy's root."""
    path = os.path.join(lab.box.co, rel)
    argv = ([PY, "-B", path] if rel.endswith(".py") else ["/bin/bash", path]) + [flag]
    calls = os.path.join(lab.calls_dir, f"direct-{lab.n}.log")
    lab.n += 1
    before = lab.snapshot()
    try:
        r = subprocess.run(argv, env=lab.env(calls), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, start_new_session=True, cwd=lab.box.co)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = "timeout", "", ""
    after = lab.snapshot()
    called = open(calls).read().splitlines() if os.path.exists(calls) else []
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    return rc, out, err, called, changed


def run_forms(td, items, tag):
    lab = Lab(td, tag)
    try:
        problem = warm(lab)
        if problem:
            return [((), problem, None)]
        out = []
        for item in items:
            if item[0] == "direct":
                out.append((item, None, run_direct(lab, item[1], item[2])))
                continue
            if item[0] == "shape":
                out.append((item, None, lab.run(list(item[1]), item[2], list(item[3]))))
                continue
            vm, cmd, args = item
            out.append(((tuple(vm), cmd, tuple(args)), None, lab.run(vm, cmd, args)))
        return out
    finally:
        lab.close()



# --- options that take a value (bin/metacomputer mc_value_options) ------------------------------------------------
# The word after one of these is its value, never a request for usage (`sh --file --help` reads the file named --help,
# as before); every other -h or --help before `--` is. The table must say what each command's own parser does.

def bash_tables():
    """mc_value_options and mc_help_subcommands for every command, as bin/metacomputer has them."""
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    found = [re.search(rf"^{n}\(\) \{{.*?^\}}$", src, re.S | re.M) for n in ("mc_value_options", "mc_help_subcommands",
                                                                           "mc_help_asked")]
    fns = "\n".join(m.group(0) if m else f"{n}() {{ :; }}"     # a checkout without one: an empty table
                    for m, n in zip(found, ("mc_value_options", "mc_help_subcommands", "mc_help_asked")))
    cmds = sorted(dispatch_commands())
    script = fns + '\nfor c in "$@"; do printf "%s\\t%s\\t%s\\n" "$c" "$(mc_value_options "$c" | tr -s " \\n" " ")" ' \
                   '"$(mc_help_subcommands "$c")"; done'
    out = subprocess.run(["/bin/bash", "-c", script, "_", *cmds], capture_output=True, text=True, timeout=60).stdout
    vals, subs = {}, {}
    for line in out.splitlines():
        c, v, s = (line.split("\t") + ["", ""])[:3]
        vals[c] = set(v.split())
        subs[c] = set(s.split())
    return vals, subs, fns


def argparse_options():
    """{prog: {option: takes a value}} and {prog: subcommand names} of the argparse entry points, from the parsers their
    main() builds when asked for --help (in this process, nothing else run)."""
    import argparse
    import contextlib
    import importlib
    import io
    sys.path.insert(0, os.path.join(LAB, "lib"))
    seen, subs = {}, {}
    real_add, real_sub = argparse.ArgumentParser.add_argument, argparse._SubParsersAction.add_parser
    flagish = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction, argparse._HelpAction,
               argparse._VersionAction, argparse._StoreConstAction, argparse._AppendConstAction)

    def add(self, *a, **k):
        act = real_add(self, *a, **k)
        for o in act.option_strings:
            seen.setdefault(self.prog, {})[o] = act.nargs != 0 and not isinstance(act, flagish)
        return act

    def add_parser(self, name, **k):
        p = real_sub(self, name, **k)
        subs.setdefault(self._prog_prefix, set()).add(name)
        return p
    argparse.ArgumentParser.add_argument, argparse._SubParsersAction.add_parser = add, add_parser
    old_env = dict(os.environ)
    try:
        os.environ["HOME"] = tempfile.mkdtemp(prefix="mchelp-parse-")
        for mod, argvs in (("events", [["--help"]]), ("handoff", [["--help"]] + [[s, "--help"] for s in ("done", "cancel",
                                                                                                        "wait", "status")]),
                           ("learn", [["--help"]]), ("agents", [["--help"]]), ("shop", [["--help"]]),
                           ("browser_tools", [["--help"]]), ("traces", [["--help"]]), ("update", [["version", "--help"]]),
                           ("storage", [["--help"]]), ("netpolicy", [["--help"]]), ("remote", [["--help"]])):
            m = importlib.import_module(mod)
            for argv in argvs:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        m.main(argv)
                    except SystemExit:
                        pass
    finally:
        argparse.ArgumentParser.add_argument, argparse._SubParsersAction.add_parser = real_add, real_sub
        shutil.rmtree(os.environ["HOME"], ignore_errors=True)
        os.environ.clear()
        os.environ.update(old_env)
    return seen, subs


def hand_parsed(rel, pattern):
    """Options of a hand-written parser that take a value: each `== "OPT"` (or `in ("A", "B")`) branch whose body (up
    to the next branch) takes the following word (pattern)."""
    src = open(os.path.join(LAB, rel)).read().splitlines()
    out = set()
    for i, line in enumerate(src):
        m = re.search(r'(?:==|in) \(?((?:"-[-a-z]+"(?:, )?)+)\)?:\s*$', line)
        if not m:
            continue
        ind = len(line) - len(line.lstrip())
        body = []
        for x in src[i + 1:i + 12]:
            if x.strip() and len(x) - len(x.lstrip()) <= ind:
                break
            body.append(x)
        if any(re.search(pattern, x) for x in body):
            out |= set(re.findall(r'"(-[-a-z]+)"', m.group(1)))
    return out


def vault_values():
    src = "".join(open(os.path.join(LAB, "vault", f)).read() for f in sorted(os.listdir(os.path.join(LAB, "vault")))
                  if f.endswith(".swift"))
    consts = {m.group(1): re.findall(r'"([a-z][a-z0-9-]*)"', m.group(2))
              for m in re.finditer(r"let (\w+)\s*(?::\s*Set<String>)?\s*=\s*(?:Set\()?\[([^\]]*)\]", src)}
    vals = set()
    for m in re.finditer(r"parseArgs\(", src):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        v = re.search(r"values:\s*(.*?)(?:,\s*flags:|$)", src[m.end():i - 1], re.S)
        if v:
            vals |= set(re.findall(r'"([a-z][a-z0-9-]*)"', v.group(1)))
            for ident in re.findall(r"\b([a-z]\w*(?:Flags|Values))\b", v.group(1)):
                vals |= set(consts.get(ident, []))
    return {"--" + x for x in vals}


def bash_fn(rel, name):
    src = open(os.path.join(LAB, rel)).read()
    m = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.S | re.M)
    return m.group(0) if m else ""


def real_value_options():
    """What each command's own parser takes a value for."""
    seen, _subs = argparse_options()

    def progs(prefix, skip=()):
        return {o for p, opts in seen.items() if p == prefix or p.startswith(prefix + " ")
                if not any(p == f"{prefix} {s}" for s in skip) for o, takes in opts.items() if takes}
    real = {
        "events": progs("metacomputer events", skip=("notify", "_recording", "watch", "has-sinks", "needs-watcher")),
        "notify": progs("metacomputer events notify"),
        "handoff": progs("metacomputer handoff"), "learn": progs("metacomputer learn"),
        "net": progs("metacomputer net", skip=("_expire", "render")), "volume": progs("metacomputer volume"),
        "refdisk": progs("metacomputer refdisk"), "shop": progs("metacomputer shop"), "trace": progs("metacomputer trace"),
        "web": progs("metacomputer web"), "update": progs("metacomputer update"), "version": progs("metacomputer version"),
        "agents": progs("metacomputer agents"), "cloud": progs("remote.py cloud"),
        "sh": hand_parsed("lib/vm_shell.py", r"value\(\)"),
        "claude": hand_parsed("lib/vm_claude.py", r"value\(\)"),
        "call": hand_parsed("lib/mcomp_sdk.py", r"next\(it"),
        "put": hand_parsed("lib/guest_put.py", r"rest\[i \+ 1\]"),
        "each": set(re.findall(r'argv\[0\]\.startswith\("(--[a-z-]+)"\)', open(os.path.join(LAB, "lib", "each.py")).read())),
        "secrets": vault_values(),
        # bin/metacomputer's and scripts/vm-commands.sh's own parsers: an option whose branch reads the next word ($2)
        "vm": set(re.findall(r'^\s*(--[a-z-]+)\) \[ \$# -ge 2 \][^\n]*"\$2"', open(os.path.join(LAB, "scripts",
                                                                                               "vm-commands.sh")).read(), re.M)),
        "list": set(re.findall(r'^\s*(--[a-z-]+)\)[^\n]*"\$2"', bash_fn("scripts/vm-commands.sh", "vm_list"), re.M)),
        "inspect": {o for g in re.findall(r'^\s*((?:--[a-z-]+\|?)+)\)[^\n]*"\$\{?2', bash_fn("bin/metacomputer", "do_inspect"),
                                          re.M) for o in g.split("|")},
        "get": set(re.findall(r'(--[a-z-]+)\) \w+="\$\{2:-\}"; shift 2', bash_fn("bin/metacomputer", "do_get"))),
        "shot": set(re.findall(r'(--[a-z-]+)\) \w+="\$\{2:-\}"; shift 2', bash_fn("bin/metacomputer", "do_shot"))),
        "browser": {o for g in re.findall(r'case "\$\{2:-\}" in\n\s*((?:--[a-z-]+\|?)+)\)', bash_fn("bin/metacomputer",
                                                                                                    "do_browser"))
                    for o in g.split("|")},
        "checkpoint": set(re.findall(r'"\$\{1:-\}" = "(--[a-z-]+)" \]; then\n\s*ckpt_name "\$\{2:-\}"',
                                     bash_fn("bin/metacomputer", "do_checkpoint"))),
    }
    return real, _subs


def value_option_checks():
    print("\noptions that take a value: bin/metacomputer's table against each command's own parser")
    vals, subs, fns = bash_tables()
    real, parsed_subs = real_value_options()
    for cmd in sorted(set(real) | {c for c, v in vals.items() if v}):
        want, have = real.get(cmd, set()), vals.get(cmd, set())
        check(f"{cmd}: mc_value_options names exactly the options its own parser takes a value for",
              want == have and (want or cmd in ("version", "agents")), (sorted(want - have), sorted(have - want)))
    # The subcommands whose own usage is asked for exist in that parser (a word not there would be an error there).
    prefix = {"volume": "metacomputer volume", "refdisk": "metacomputer refdisk", "events": "metacomputer events",
              "learn": "metacomputer learn", "shop": "metacomputer shop", "web": "metacomputer web",
              "trace": "metacomputer trace"}
    for cmd, words in sorted(subs.items()):
        if not words:
            continue
        if cmd == "handoff":
            ok = words == {"done", "cancel", "wait", "status"}
        else:
            known = parsed_subs.get(prefix[cmd], set()) | {f"sink:{s}" for s in parsed_subs.get("metacomputer events sink", ())}
            ok = words <= known
        check(f"{cmd}: mc_help_subcommands names only subcommands its parser has", ok, sorted(words))
    # The scanner: an option's value is never a request for usage; -h or --help in a flag's place always is.
    pairs = sorted({(c, o) for c, v in vals.items() for o in v}
                   | {("sh", "--file"), ("sh", "-c"), ("handoff", "--note"), ("put", "--to"), ("secrets", "--field"),
                      ("vm", "--confirm"), ("volume", "--size")})
    script = fns + '\nwhile IFS=$\'\\t\' read -r c o; do\n' \
                   '  mc_help_asked "$c" "$o" --help && a=help || a=value\n' \
                   '  mc_help_asked "$c" "$o" x --help && b=help || b=value\n' \
                   '  mc_help_asked "$c" "$o" x -h && d=help || d=value\n' \
                   '  printf "%s %s %s %s %s\\n" "$c" "$o" "$a" "$b" "$d"\ndone'
    out = subprocess.run(["/bin/bash", "-c", script], input="".join(f"{c}\t{o}\n" for c, o in pairs),
                         capture_output=True, text=True, timeout=120).stdout.split("\n")
    wrong = [ln for ln in out if ln and ln.split()[2:] != ["value", "help", "help"]]
    check(f"the early scanner: after each of the {len(pairs)} options that take a value, --help is that value "
          "(`sh --file --help` reads the file named --help); after the value, --help and -h ask for usage",
          len([ln for ln in out if ln]) == len(pairs) and not wrong, wrong[:6])


# The P1 shape (codex audit, fix-cli round 2): an option that takes a value, then --help, then a word that would act
# (`handoff --transport 1 --help cancel` once cancelled the open handoff through the help path). For every command and
# every such option, nothing acts: the help path passes on fixed words only, and where --help is the option's value
# the command's own parser gets it as before and refuses the rest.
ACTING_WORD = {"sh": "rm", "claude": "stop", "call": "click", "each": "reset", "put": "f", "get": "x", "shot": "x",
               "inspect": "windows", "browser": "--yes", "checkpoint": "ck", "vm": "delete", "list": "delete",
               "cloud": "teardown", "handoff": "cancel", "notify": "hi", "events": "remove", "learn": "remove",
               "net": "clear", "volume": "delete", "refdisk": "delete", "shop": "complete", "trace": "delete",
               "update": "--yes", "web": "open-untrusted", "secrets": "delete"}


def shape_forms():
    """Every option that takes a value, by its command's own parser and by the table, then --help, then a word that
    would act."""
    vals, _subs, _fns = bash_tables()
    real, _ = real_value_options()
    out = [([], "handoff", ["--transport", "1", "--help", "cancel"]), ([], "handoff", ["--transport", "1", "-h", "done"])]
    for c in sorted(set(vals) | set(real)):
        for o in sorted(vals.get(c, set()) | real.get(c, set())):
            out.append(([], c, [o, "--help", ACTING_WORD.get(c, "delete"), "x"]))
    return out


def acting(called):
    """The calls that act: anything but the runner's lists and lookups, and the vault asked for its version or refusing
    a first word that's an option (it takes its command from its first word: `unknown command`)."""
    out = []
    for c in called:
        w = c.split()
        if w[:2] == ["standin", "mcomp-vm"] and (w[2:3] in (["get"], ["ls"]) or w[2:4] == ["config", "get"]):
            continue
        if w[1:2] in (["mcomp-vault"], ["vault-stand-in"]) and (w[2:3] == ["version"]
                                                                 or (len(w) > 2 and w[2].startswith("-"))):
            continue
        out.append(c)
    return out


def main():
    td = tempfile.mkdtemp(prefix="mchelp-", dir="/tmp")
    try:
        # Every command of the dispatch has rows here (a new one fails until it has).
        have = {cmd for _vm, cmd, _a in FORMS}
        missing = sorted(dispatch_commands() - have)
        check("every command of bin/metacomputer's dispatch has --help rows here", not missing, missing)

        value_option_checks()

        todo = (forms() + [("direct", rel, f) for rel in direct_targets() for f in FLAGS]
                + [("shape", tuple(vm), cmd, tuple(a)) for vm, cmd, a in shape_forms()])
        workers = max(2, min(8, (os.cpu_count() or 4)))
        parts = [todo[i::workers] for i in range(workers)]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(run_forms, td, p, f"w{i}") for i, p in enumerate(parts)]
            for f in futs:
                results.extend(f.result())
        for key, problem, res in results:
            if problem:
                check("the copy runs a plain command first (nothing called)", False, problem)
                continue
            if key[0] == "shape":
                _, vm, cmd, args = key
                rc, out, err, called, changed = res
                words = " ".join(["metacomputer", *vm, cmd, *args])
                check(f"{words}: nothing acts (--help never an option's value followed by an action)",
                      rc != "timeout" and not acting(called), (rc, acting(called)[:4], err[-300:]))
                continue
            if key[0] == "direct":
                _, rel, flag = key
                rc, out, err, called, changed = res
                words = f"{rel} {flag} (run on its own)"
                if rel.startswith("scripts/") or rel in PERSON_RUNS:
                    check(f"{words}: its usage on stdout, exit 0", rc == 0 and out.strip() != "", (rc, out[-200:], err[-400:]))
                else:
                    check(f"{words}: ends by itself", rc != "timeout", (rc, err[-300:]))
                check(f"{words}: nothing called (VM, runner, ssh, provider, vault, network)", not called, called[:6])
                check(f"{words}: nothing written", not changed, changed[:6])
                continue
            vm, cmd, args = key
            rc, out, err, called, changed = res
            words = " ".join(["metacomputer", *vm, cmd, *args])
            text = out.lower()
            check(f"{words}: usage on stdout, exit 0", rc == 0 and ("usage" in text or "metacomputer" in text)
                  and out.strip() != "", (rc, out[-300:], err[-500:]))
            check(f"{words}: nothing called (VM, runner, ssh, provider, vault, network)", not called, called[:6])
            check(f"{words}: nothing written", not changed, changed[:6])

        # --help where it belongs to the guest's command or the script: not metacomputer's usage, and the stand-ins
        # show it went on to the VM's side (nothing real is reached).
        lab = Lab(td, "nothelp")
        try:
            warm(lab)
            for vm, cmd, args in NOT_HELP:
                for f in FLAGS:
                    a = [f if x == H else x for x in args]
                    rc, out, err, called, _ = lab.run(vm, cmd, a)
                    words = " ".join(["metacomputer", *vm, cmd, *a])
                    check(f"{words}: the guest's (or script's) own {f}, not metacomputer's usage",
                          "usage: metacomputer" not in out.lower() and rc != 0, (rc, out[-300:], err[-300:]))
        finally:
            lab.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{COUNT[0] - len(FAILS)}/{COUNT[0]} passed")
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
