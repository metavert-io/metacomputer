#!/usr/bin/env python3
"""Offline checks for `metacomputer claude` (lib/vm_claude.py, lib/vm_claude_guest.sh).

No VM, no vault, no Claude Code, no network. Stand-ins:
- the VM: `bash -c` on this Mac, so the real VM half of the shell channel and the real
  vm_claude_guest.sh run locally, with HOME in a temp folder;
- the vault: a fake `metacomputer` (MCOMP_SELF) that answers `secrets list|token|tokens|revoke`
  with a dummy token and records its arguments;
- Claude Code: a fake `claude` that records its arguments and environment (hashes only), turns
  on bracketed paste, and writes down every prompt it gets;
- curl (Anthropic's installer, which installs the fake), xfce4-terminal (records its arguments).
tmux is the real one when this Mac has it (Homebrew's), on a private socket folder; without it the
checks that need a running session are skipped and say so.

Run:
    python3 tests/test_vm_claude.py
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import vm_claude as vc  # noqa: E402

CLAUDE_PY = os.path.join(LAB, "lib", "vm_claude.py")
GUEST = os.path.join(LAB, "lib", "vm_claude_guest.sh")
TOKEN = "mct_" + "Q" * 6 + "dummyTokenValue0123456789"   # 35 characters after mct_, like the vault's 32
results = []
skipped = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond and detail != "":
        print("      " + str(detail)[:1500])


def skip(name, why):
    skipped.append(name)
    print(f"SKIP  {name} ({why})")


# --- stand-ins -------------------------------------------------------------------------------
FAKE_VAULT = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_VAULT_DIR"]
with open(os.path.join(d, "argv.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = open(os.path.join(d, "mode")).read().strip() if os.path.exists(os.path.join(d, "mode")) else "oauth"
token = open(os.path.join(d, "token")).read().strip()
a = sys.argv[1:]
if a[:1] != ["secrets"]:
    sys.exit(2)
sub = a[1] if len(a) > 1 else ""
secrets = {
    "oauth": [{"name": "claude-sub", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"}],
    "apikey": [{"name": "claude-key", "kind": "api", "api_host": "api.anthropic.com", "api_header": "x-api-key"}],
    "both": [{"name": "claude-sub", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"},
             {"name": "claude-key", "kind": "api", "api_host": "api.anthropic.com", "api_header": "x-api-key"}],
    "other": [{"name": "gem", "kind": "api", "api_host": "generativelanguage.googleapis.com", "api_header": "x-goog-api-key"}],
    "none": [{"name": "site", "kind": "password", "origins": ["https://example.com"]}],
    "unbound": [{"name": "claude-sub", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"}],
    "refuse": [{"name": "claude-sub", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"}],
    "badheader": [{"name": "claude-sub", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"}],
}
if mode == "locked":
    print(json.dumps({"ok": False, "reason": "locked"}))
    sys.exit(3)
if sub == "list":
    vms = [] if mode == "unbound" else [{"name": "testvm", "proxy_user": "mcproxy"}]
    print(json.dumps({"secrets": secrets.get(mode, []), "vms": vms, "sessions": []}, indent=1))
    sys.exit(0)
issued_ = open(os.path.join(d, "issued")).read().split() if os.path.exists(os.path.join(d, "issued")) else []
revoked_ = open(os.path.join(d, "revoked")).read().split() if os.path.exists(os.path.join(d, "revoked")) else []
if sub == "token" and a[2:3] == ["renew"]:
    if mode == "norenew":           # a vault from before `token renew`: the id makes it a usage error
        print("mcomp-vault: usage: token NAME --vm VM [--ttl 15m]", file=sys.stderr)
        sys.exit(64)
    tid = a[3]
    if tid not in issued_ or tid in revoked_:
        print(json.dumps({"ok": False, "token_id": tid, "reason": "no live token %s on VM testvm" % tid}))
        sys.exit(2)
    open(os.path.join(d, "renewed"), "a").write(tid + "\n")
    print(json.dumps({"ok": True, "vm": "testvm", "token_id": tid, "expires": "2026-11-19T01:00:00-05:00",
                      "expires_in_s": 7200, "extended": True, "renewals": 1, "longest_until": "2026-11-19T11:00:00-05:00"}))
    sys.exit(0)
if sub == "token":
    name = a[2]
    if mode == "refuse":
        print(json.dumps({"ok": False, "name": name, "reason": "refused at the Touch ID dialog"}))
        sys.exit(2)
    n = len(open(os.path.join(d, "argv.log")).read().splitlines())
    tid = "%08x" % (0xa0000000 + n)
    s = next((x for x in secrets.get(mode, []) if x["name"] == name), None)
    hdr = ("x-api-key: " + token) if s and s["api_header"] == "x-api-key" else ("Authorization: Bearer " + token)
    if mode == "badheader":
        hdr = "Authorization: Token " + token
    open(os.path.join(d, "issued"), "a").write(tid + "\n")
    print(json.dumps({"ok": True, "name": name, "vm": "testvm", "token_id": tid, "token": token,
                      "base_url": "http://127.0.0.1:18765/%s/" % name, "header": hdr, "api_host": "api.anthropic.com",
                      "expires": "2026-11-18T23:00:00Z", "expires_in_s": 3600}))
    sys.exit(0)
if sub == "tokens":
    issued = open(os.path.join(d, "issued")).read().split() if os.path.exists(os.path.join(d, "issued")) else []
    revoked = open(os.path.join(d, "revoked")).read().split() if os.path.exists(os.path.join(d, "revoked")) else []
    print(json.dumps({"tokens": [{"id": t, "name": "claude-sub", "vm": "testvm", "expires_in_s": 120 if mode == "soon" else 3000}
                                 for t in issued if t not in revoked]}, indent=1))
    sys.exit(0)
if sub == "revoke":            # the real vault answers in words, exit 0 either way
    issued = open(os.path.join(d, "issued")).read().split() if os.path.exists(os.path.join(d, "issued")) else []
    if mode == "norevoke" or a[2] not in issued:
        print("No such live token." if mode != "norevoke" else "")
        sys.exit(0 if mode != "norevoke" else 4)
    open(os.path.join(d, "revoked"), "a").write(a[2] + "\n")
    print("Revoked %s." % a[2])
    sys.exit(0)
sys.exit(4)
'''

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import hashlib, json, os, select, sys, time, tty
if sys.argv[1:2] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
home = os.environ["HOME"]
def sha(v):
    return hashlib.sha256(v.encode()).hexdigest() if v else None
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "oauth_sha": sha(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
       "api_sha": sha(os.environ.get("ANTHROPIC_API_KEY")), "auth_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
       "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
       "nonessential": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
       "mcomp_vars": sorted(k for k in os.environ if k.startswith("MCOMP_CLAUDE") or k == "MCOMP_KEY_SUFFIX"),
       "umask": oct(os.umask(0o022))}
json.dump(rec, open(os.path.join(home, "fake-claude.json"), "w"))
mode = open(os.path.join(home, "fake-claude-mode")).read().strip() if os.path.exists(os.path.join(home, "fake-claude-mode")) else ""
out = sys.stdout
if mode == "die":
    out.write("Invalid API key \x1b]0;evil-title\x07 Please run /login\n")
    out.flush()
    sys.exit(1)
tty.setraw(0)
out.write("\x1b[?2004hfake claude ready\r\n> ")
out.flush()
buf, paste, data = "", False, b""
inbox = os.path.join(home, "fake-claude-inbox.jsonl")
def submit(text):
    with open(inbox, "a") as f:
        f.write(json.dumps(text) + "\n")
    out.write("\r\nGOT[%d]: %s\r\n" % (len(text), text.replace("\n", " | ")))
    if text == "hostile":
        out.write("look \x1b]52;c;SGVsbG8=\x07 here \u202eevil\u202c\r\n")
    if text == "slow":
        for i in range(6):
            out.write("working %d\r\n" % i); out.flush(); time.sleep(0.4)
        out.write("DONE\r\n")
    if text == "quit":
        out.write("bye\r\n"); out.flush(); sys.exit(0)
    out.write("> ")
    out.flush()
while True:
    r, _, _ = select.select([0], [], [], 0.2)
    if not r:
        continue
    chunk = os.read(0, 4096)
    if not chunk:
        break
    data += chunk
    while data:
        if data.startswith(b"\x1b[200~"):
            paste, data = True, data[6:]; continue
        if data.startswith(b"\x1b[201~"):
            paste, data = False, data[6:]; continue
        if data[:1] == b"\x1b" and len(data) < 6 and not paste:
            if len(data) == 1:
                out.write("\r\nINTERRUPTED\r\n> "); out.flush(); data = b""; buf = ""
            break
        c, data = data[:1], data[1:]
        ch = c.decode("latin-1")
        if not paste and ch in "\r\n":
            submit(buf); buf = ""
        else:
            buf += "\n" if (paste and ch == "\r") else ch
'''

FAKE_CURL = r'''#!/bin/bash
# curl for Anthropic's installer: writes an install.sh that puts the fake claude in ~/.local/bin
echo "$*" >> "$HOME/curl.log"
out=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; esac; shift; done
[ -n "$out" ] || exit 22
cat > "$out" <<EOF
mkdir -p "\$HOME/.local/bin" && cp "$FAKE_CLAUDE_SRC" "\$HOME/.local/bin/claude" && chmod 755 "\$HOME/.local/bin/claude"
echo "installed fake claude"
EOF
'''

FAKE_TERMINAL = r'''#!/bin/bash
printf '%s\n' "DISPLAY=${DISPLAY:-}" "$@" > "$HOME/terminal-argv.txt"
'''


def tmux_real():
    for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def setup(td, with_claude=True):
    home, bin_, vault = (os.path.join(td, x) for x in ("home", "bin", "vault"))
    for d in (home, bin_, vault, os.path.join(td, "run"), os.path.join(td, "xdg", "metacomputer"), os.path.join(td, "tmux")):
        os.makedirs(d, exist_ok=True)
    os.chmod(os.path.join(td, "tmux"), 0o700)

    def put(path, text, mode=0o755):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)

    put(os.path.join(vault, "token"), TOKEN + "\n", 0o600)
    put(os.path.join(td, "fake-vault"), FAKE_VAULT)
    put(os.path.join(td, "fake-claude"), FAKE_CLAUDE)
    if with_claude:
        shutil.copy(os.path.join(td, "fake-claude"), os.path.join(bin_, "claude"))
    put(os.path.join(bin_, "curl"), FAKE_CURL)
    put(os.path.join(bin_, "xfce4-terminal"), FAKE_TERMINAL)
    real = tmux_real()
    if real:
        # Every tmux call is recorded: no token may be among its arguments. With td/hostile present, capture-pane
        # also prints raw escape sequences, as a VM whose tmux was replaced could (the real one interprets them).
        put(os.path.join(bin_, "tmux"), f'''#!/bin/bash
printf "%s\\n" "$*" >> "{td}/tmux-argv.log"
case " $* " in *" capture-pane "*)
  if [ -e "{td}/hostile" ]; then {real} "$@"; printf 'raw \\033]52;c;SGVsbG8=\\007 osc \\033[2J clear \\342\\200\\256bidi\\n'; exit 0; fi ;;
esac
exec {real} "$@"
''')
    # The desktop session's environment, as the VM's half of the shell channel loads it
    put(os.path.join(td, "xdg", "metacomputer", "session.env"), "DISPLAY=:99\n", 0o644)
    return home


def env_for(td, **extra):
    env = {k: v for k, v in os.environ.items() if not (k.startswith("MCOMP_") or k.startswith("FAKE_") or k in ("TMUX", "TMUX_PANE"))}
    env.update(HOME=os.path.join(td, "home"), PATH=os.path.join(td, "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
               TMUX_TMPDIR=os.path.join(td, "tmux"), MCOMP_RUN_DIR=os.path.join(td, "run"),
               MCOMP_ACTION_LOG_DIR=os.path.join(td, "actions"), MCOMP_ACTION_LOG_KEY=os.path.join(td, "keys", "action-log"),
               MCOMP_VM_NAME="testvm", MCOMP_GUEST_USER="cua", MCOMP_SSH_PORT="1", XDG_RUNTIME_DIR=os.path.join(td, "xdg"),
               MCOMP_SELF=os.path.join(td, "fake-vault"), FAKE_VAULT_DIR=os.path.join(td, "vault"),
               FAKE_CLAUDE_SRC=os.path.join(td, "fake-claude"), MCOMP_SH_GRACE_S="5")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def mc(td, *args, stdin=None, timeout=120, **env):
    return subprocess.run([sys.executable, CLAUDE_PY, "--transport", "2", "bash", "-c", *args], input=stdin,
                          capture_output=True, text=True, timeout=timeout, env=env_for(td, **env))


def js(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def vault_calls(td):
    p = os.path.join(td, "vault", "argv.log")
    return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


def set_mode(td, mode):
    open(os.path.join(td, "vault", "mode"), "w").write(mode)


def claude_rec(td):
    try:
        return json.load(open(os.path.join(td, "home", "fake-claude.json")))
    except (OSError, ValueError):
        return {}


def inbox(td):
    p = os.path.join(td, "home", "fake-claude-inbox.jsonl")
    return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def every_file(root):
    for d, _, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p):
                yield p


def files_with(root, needle, skip=()):
    hits = []
    for p in every_file(root):
        if any(p.startswith(s) for s in skip):
            continue
        try:
            if needle.encode() in open(p, "rb").read():
                hits.append(os.path.relpath(p, root))
        except OSError:
            pass
    return hits


# --- checks without a session -------------------------------------------------------------------
def unit_checks():
    p = vc.parse
    o = p([])
    check("no action is start, session claude, token picked later, 1 h, ~/claude-work",
          o["action"] == "start" and o["session"] == "claude" and o["token"] is None and o["ttl_s"] == 3600
          and o["cwd"] == "~/claude-work" and o["install"] and not o["skip"] and not o["show"])
    o = p(["--token", "claude-sub", "--ttl", "30m", "--model", "sonnet", "--skip-permissions", "--show",
           "--session", "two", "--", "--continue", "--add-dir", "/tmp"])
    check("start options, and Claude Code's own arguments after --",
          o["token"] == "claude-sub" and o["ttl_s"] == 1800 and o["model"] == "sonnet" and o["skip"] and o["show"]
          and o["session"] == "two" and o["claude_args"] == ["--continue", "--add-dir", "/tmp"])

    def bad(argv, what):
        try:
            vc.parse(argv)
            check(f"usage error: {what}", False, argv)
        except vc.Usage:
            check(f"usage error: {what}", True)

    bad(["--session", "Bad_Name"], "session names are lowercase, digits and dashes")
    bad(["--session", "-x"], "a session name can't start with a dash")
    bad(["--ttl", "9h"], "a ttl over 8 h")
    bad(["--ttl", "30s"], "a ttl under a minute")
    bad(["--ttl", "1d"], "a ttl in days")
    bad(["--token", "-rm"], "a secret name that looks like an option")
    bad(["--token", "a b"], "a secret name with a space")
    bad(["--model", "x;rm -rf ~"], "a model name with shell characters")
    bad(["--cwd", "relative"], "a relative --cwd")
    bad(["--cwd", "~/../etc"], "a --cwd that climbs out with ..")
    bad(["--cwd", "/tmp/$(id)"], "a --cwd with shell characters")
    bad(["start", "extra"], "start takes no positional arguments")
    bad(["read", "--token", "x"], "an option of another action")
    bad(["read", "--lines", "0"], "--lines below 1")
    bad(["wait", "--timeout", "2", "--quiet", "5"], "--timeout shorter than --quiet")
    bad(["send", "a", "b"], "send takes one TEXT")
    bad(["send", "--no-enter"], "send with nothing to send")
    bad(["keys"], "keys needs a key")
    bad(["keys", "rm -rf"], "keys refuses text")
    bad(["keys", "F13"], "keys refuses names outside its list")
    bad(["keys", "C-?"], "keys refuses odd control names")
    bad(["status", "--", "x"], "-- outside start and send")
    bad(["renew", "--ttl", "9h"], "renew past 8 h")
    bad(["renew", "abcd1234"], "renew takes the session, not a token id")
    bad(["renew", "--token", "x"], "renew doesn't take --token")
    o = p(["renew", "--ttl", "90m", "--session", "two"])
    check("renew --ttl 90m --session two", o["action"] == "renew" and o["ttl_s"] == 5400 and o["session"] == "two")
    check("renew without --ttl leaves it unset (the vault's step)", p(["renew"])["ttl"] is None)
    bad(["frobnicate"], "an unknown action")
    o = p(["send", "--", "--looks like an option"])
    check("send -- TEXT takes text that starts with dashes", o["text"] == "--looks like an option" and o["text_source"] == "arg")
    o = p(["send"])
    check("send with no text just presses Enter", o["text_source"] == "none" and o["enter"])
    o = p(["keys", "Escape", "C-c", "1", "BTab", "Enter", "y"])
    check("keys: named keys, C-x and single characters", o["keys"] == ["Escape", "C-c", "1", "BTab", "Enter", "y"])
    o = p(["send", "-"])
    import io
    check("send - reads the text from stdin", vc.read_text(o, io.BytesIO("from stdin\n".encode())) == "from stdin\n")
    try:
        vc.read_text(p(["send", "-"]), io.BytesIO(b"x" * (vc.MAX_TEXT + 1)))
        check("send refuses text over the limit", False)
    except vc.Usage:
        check("send refuses text over the limit", True)
    try:
        vc.read_text(p(["send", "-"]), io.BytesIO(b"\xff\xfe"))
        check("send refuses text that isn't UTF-8", False)
    except vc.Usage:
        check("send refuses text that isn't UTF-8", True)

    t = TOKEN
    check("header -> how Claude Code sends it: Authorization: Bearer is a subscription token (CLAUDE_CODE_OAUTH_TOKEN)",
          vc.auth_for_header("Authorization: Bearer " + t, t) == "oauth" and vc.env_var_for("oauth") == "CLAUDE_CODE_OAUTH_TOKEN"
          and vc.auth_for_header("authorization:   Bearer " + t, t) == "oauth")
    check("header -> x-api-key is an API key (ANTHROPIC_API_KEY)",
          vc.auth_for_header("x-api-key: " + t, t) == "api-key" and vc.auth_for_header("X-Api-Key: " + t, t) == "api-key"
          and vc.env_var_for("api-key") == "ANTHROPIC_API_KEY")
    check("header -> anything else is refused (another scheme, another header, another value, a suffix)",
          all(vc.auth_for_header(h, t) is None for h in (
              "Authorization: Token " + t, "Authorization: Bearer other", "x-goog-api-key: " + t,
              "x-api-key: " + t + "x", "x-api-key: Bearer " + t, "Authorization " + t, "", None)))
    lst = {"secrets": [{"name": "a", "kind": "api", "api_host": "api.anthropic.com", "api_header": "Authorization"},
                       {"name": "b", "kind": "password"},
                       {"name": "g", "kind": "api", "api_host": "x.example", "api_header": "x-goog-api-key"}]}
    check("without --token, the one api secret for api.anthropic.com", vc.pick_secret(lst, None)[0] == "a")
    for name, listing, code, what in (
            ("b", lst, vc.EXIT_REFUSED, "a password secret"), ("g", lst, vc.EXIT_REFUSED, "a header Claude Code can't send"),
            ("zz", lst, vc.EXIT_REFUSED, "no such secret"),
            (None, {"secrets": []}, vc.EXIT_REFUSED, "no anthropic secret at all"),
            (None, {"secrets": lst["secrets"] + [{"name": "c", "kind": "api", "api_host": "api.anthropic.com",
                                                  "api_header": "x-api-key"}]}, vc.EXIT_USAGE, "two to choose from")):
        try:
            vc.pick_secret(listing, name)
            check(f"pick_secret refuses {what}", False)
        except vc.Failed as f:
            check(f"pick_secret refuses {what} ({f.code})", f.code == code and (what != "no anthropic secret at all"
                                                                              or "secrets set" in f.message), f.message)

    raw = ("@mc state running\n@mc bogus x\n@mc viewers 1\x1b[2J\n@mc size 200x50\n@mc screen-begin\n"
           "@mc state exited\nline \x1b]52;c;QQ==\x07\n@mc screen-end\n@mc session one\n@mc session two\n").encode()
    f, s = vc.parse_output(raw)
    check("the VM's fields: known names with plain values only; field-like lines inside the screen stay screen text",
          f == {"state": "running", "size": "200x50", "sessions": ["one", "two"]} and "@mc state exited" in s
          and "\x1b]52" in s, (f, s))
    check("escaping: control characters, OSC 52 and bidi overrides are shown as escapes",
          vc.clean("a\x1b]52;c;QQ==\x07b\u202ec") == "a\\x1b]52;c;QQ==\\x07b\\u202ec")


# --- checks with a session (tmux) ----------------------------------------------------------------
def session_checks(td):
    home = setup(td)
    set_mode(td, "oauth")
    leaks = []                  # (where, text) for every output that must not hold the token

    r = mc(td, "--token", "claude-sub", "--model", "sonnet", "--", "--add-dir", "/tmp", timeout=120,
           MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_TEXT="keep")
    leaks.append(("start output", r.stdout + r.stderr))
    rec = claude_rec(td)
    want_sha = hashlib.sha256(TOKEN.encode()).hexdigest()
    check("start: exit 0, says the session runs and names the token by id only", r.returncode == 0
          and "Claude Code is running" in r.stdout and "token a" in r.stdout, r.stdout + r.stderr)
    check("start: Claude Code got the token as CLAUDE_CODE_OAUTH_TOKEN (Authorization: Bearer), and no API key",
          rec.get("oauth_sha") == want_sha and rec.get("api_sha") is None and rec.get("auth_token") is False, rec)
    check("start: ANTHROPIC_BASE_URL is the proxy in the VM, without the trailing slash; nonessential traffic off",
          rec.get("base_url") == "http://127.0.0.1:18765/claude-sub" and rec.get("nonessential") == "1", rec)
    check("start: --model and the arguments after -- reach Claude Code; it runs in ~/claude-work",
          rec.get("argv") == ["--model", "sonnet", "--add-dir", "/tmp"]
          and os.path.realpath(rec.get("cwd", "")) == os.path.realpath(os.path.join(home, "claude-work")), rec)
    check("start: nothing named MCOMP_CLAUDE* (or the key's suffix) reaches Claude Code's environment; umask 022",
          rec.get("mcomp_vars") == [] and rec.get("umask") == "0o22", rec)
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    wd = os.path.realpath(os.path.join(home, "claude-work"))
    check("first-run choices in ~/.claude.json: onboarding done, a theme, the folder trusted; no API-key answer for a "
          "subscription token", cfg.get("hasCompletedOnboarding") is True and cfg.get("theme") == "dark"
          and (cfg.get("projects") or {}).get(wd, {}).get("hasTrustDialogAccepted") is True
          and "customApiKeyResponses" not in cfg and stat.S_IMODE(os.stat(os.path.join(home, ".claude.json")).st_mode) == 0o600,
          cfg)
    state = os.path.join(home, ".local", "state", "metacomputer", "claude", "claude")
    check("the token's file in the VM is gone once Claude Code read it; the launcher holds no token; the folder is 0700",
          not os.path.exists(os.path.join(state, "env")) and TOKEN not in open(os.path.join(state, "launch.sh")).read()
          and stat.S_IMODE(os.stat(state).st_mode) == 0o700, os.listdir(state))
    st = os.path.join(td, "run", "claude", "claude.json")
    srec = json.load(open(st)) if os.path.exists(st) else {}
    check("the Mac's record of the session: token id, secret, env var; never the token; 0600",
          srec.get("token_id", "").startswith("a") and srec.get("secret") == "claude-sub"
          and srec.get("env_var") == "CLAUDE_CODE_OAUTH_TOKEN" and TOKEN not in open(st).read()
          and stat.S_IMODE(os.stat(st).st_mode) == 0o600, srec)
    calls = vault_calls(td)
    check("the vault was asked for a list, then one token, with the secret's name and --ttl 1h (never a token in its "
          "arguments)", calls[:2] == [["secrets", "list", "--json"], ["secrets", "token", "claude-sub", "--ttl", "1h"]]
          and not any(TOKEN in json.dumps(c) for c in calls), calls)
    path_lines = open(os.path.join(home, ".bashrc")).read().count("# metacomputer: Claude Code (~/.local/bin)")
    check("~/.local/bin goes on the desktop user's PATH (~/.bashrc), once", path_lines == 1, path_lines)

    # --- send / read / wait / keys ---
    r = mc(td, "send", "hello world")
    check("send: one line arrives whole, then Enter submits it", r.returncode == 0 and wait_for(lambda: inbox(td)[-1:] == ["hello world"]),
          (r.stdout + r.stderr, inbox(td)))
    multi = "line one\nline two with  spaces\n\tand a tab; $(not run) `nor this`"
    r = mc(td, "send", "-", stdin=multi)
    check("send -: several lines arrive as one prompt (a bracketed paste), nothing in them expanded",
          r.returncode == 0 and wait_for(lambda: inbox(td)[-1:] == [multi]), (r.stderr, inbox(td)[-1:]))
    r = mc(td, "send", "draft only", "--no-enter")
    time.sleep(1)
    n_before = len(inbox(td))
    r2 = mc(td, "keys", "Enter")
    check("send --no-enter types without submitting; keys Enter submits it", r.returncode == 0 and r2.returncode == 0
          and n_before == 2 and wait_for(lambda: inbox(td)[-1:] == ["draft only"]), (n_before, inbox(td)))
    r = mc(td, "keys", "4", "2", "Enter")
    check("keys: digits are typed literally", r.returncode == 0 and wait_for(lambda: inbox(td)[-1:] == ["42"]), inbox(td)[-2:])
    r = mc(td, "send", "slow", "--wait", "--quiet", "2", "--timeout", "60")
    check("send --wait: returns once the screen holds still, with the screen (the work finished first)",
          r.returncode == 0 and "DONE" in r.stdout and "working 5" in r.stdout, r.stdout[-600:] + r.stderr)
    r = mc(td, "send", "hostile")
    time.sleep(1.5)
    r = mc(td, "read", "--lines", "15")
    check("read: a bidi override Claude Code printed comes back escaped (\\u202e); tmux itself interpreted the "
          "escape sequences", r.returncode == 0 and "here \\u202eevil" in r.stdout and "\u202e" not in r.stdout
          and "\x1b" not in r.stdout, repr(r.stdout[-500:]))
    open(os.path.join(td, "hostile"), "w").close()        # a VM whose tmux hands back raw escape sequences
    r = mc(td, "read", "--lines", "15")
    rj = mc(td, "read", "--json")
    os.remove(os.path.join(td, "hostile"))
    check("read from a hostile VM: OSC 52, a screen clear and a bidi override come back escaped, never raw",
          r.returncode == 0 and "raw \\x1b]52;c;SGVsbG8=\\x07 osc \\x1b[2J clear \\u202ebidi" in r.stdout
          and "\x1b" not in r.stdout and "\u202e" not in r.stdout, repr(r.stdout[-300:]))
    check("read --json from it: escaped too", rj.returncode == 0 and "\\\\x1b]52" in rj.stdout and "\x1b" not in rj.stdout
          and "\\\\u202ebidi" in rj.stdout, rj.stdout[-300:])
    r = mc(td, "wait", "--quiet", "1", "--timeout", "30", "--lines", "5")
    check("wait: the screen is still, so it returns at once with the last lines", r.returncode == 0 and "> " in r.stdout,
          r.stdout + r.stderr)
    r = mc(td, "keys", "Escape")
    check("keys Escape reaches it as a lone Escape", r.returncode == 0
          and wait_for(lambda: "INTERRUPTED" in mc(td, "read").stdout), r.stderr)

    # --- status / show / a second start ---
    r = mc(td, "status", "--json")
    d = js(r)
    check("status: running, the token live (the vault's list), Claude Code's version", r.returncode == 0
          and d.get("state") == "running" and (d.get("token") or {}).get("live") is True and d.get("claude") == "9.9.9 (Claude Code)",
          r.stdout)
    r = mc(td, "show")
    tfile = os.path.join(home, "terminal-argv.txt")
    wait_for(lambda: os.path.exists(tfile) and open(tfile).read().endswith("\n"), 10)
    targv = open(tfile).read().split("\n") if os.path.exists(tfile) else []
    check("show: a terminal on the desktop (its DISPLAY) runs tmux attach to the session",
          r.returncode == 0 and targv[:1] == ["DISPLAY=:99"] and "attach" in targv and "=claude" in targv
          and "metacomputer-claude" in targv, (r.stdout + r.stderr, targv))
    n = len(vault_calls(td))
    r = mc(td)
    check("start while it runs: refused (125) before the vault is asked for a token", r.returncode == 125
          and "already running" in r.stderr and not any(c[1] == "token" for c in vault_calls(td)[n:]),
          (r.stderr, vault_calls(td)[n:]))

    # --- a second session, an API key ---
    set_mode(td, "apikey")
    r = mc(td, "--session", "two", "--token", "claude-key", "--skip-permissions", "--cwd", "~/other work")
    rec2 = claude_rec(td)
    cfg = json.load(open(os.path.join(home, ".claude.json")))
    wd2 = os.path.realpath(os.path.join(home, "other work"))
    check("an x-api-key secret: the token goes in ANTHROPIC_API_KEY, not CLAUDE_CODE_OAUTH_TOKEN",
          r.returncode == 0 and rec2.get("api_sha") == want_sha and rec2.get("oauth_sha") is None, (r.stderr, rec2))
    check("its API-key prompt is answered: customApiKeyResponses.approved holds the token's last 20 characters only",
          TOKEN[-20:] in (cfg.get("customApiKeyResponses") or {}).get("approved", [])
          and TOKEN not in json.dumps(cfg), cfg.get("customApiKeyResponses"))
    st2 = json.load(open(os.path.join(home, ".claude", "settings.json")))
    check("--skip-permissions: --dangerously-skip-permissions, and its warning pre-accepted for this VM's Claude Code",
          rec2.get("argv") == ["--dangerously-skip-permissions"] and st2.get("skipDangerousModePermissionPrompt") is True
          and cfg.get("bypassPermissionsModeAccepted") is True, (rec2, st2))
    check("--cwd with a space: trusted and used", (cfg.get("projects") or {}).get(wd2, {}).get("hasTrustDialogAccepted") is True
          and os.path.realpath(rec2.get("cwd", "")) == wd2, rec2)
    r = mc(td, "status", "--json")
    check("status lists both sessions", sorted(js(r).get("sessions") or []) == ["claude", "two"], r.stdout)

    # --- renewing the token, and being told when it runs low ---
    tid1 = json.load(open(os.path.join(td, "run", "claude", "claude.json")))["token_id"]
    n = len(vault_calls(td))
    r = mc(td, "renew", "--ttl", "2h")
    rec1 = json.load(open(os.path.join(td, "run", "claude", "claude.json")))
    check("renew: `secrets token renew ID --ttl 2h` for the session's token; the Mac's record gets the new expiry",
          r.returncode == 0 and "now lives until 2026-11-19T01:00:00-05:00" in r.stdout
          and vault_calls(td)[n:] == [["secrets", "token", "renew", tid1, "--ttl", "2h"]]
          and rec1.get("expires") == "2026-11-19T01:00:00-05:00", (r.stdout + r.stderr, vault_calls(td)[n:]))
    n = len(vault_calls(td))
    r = mc(td, "renew", "--session", "two")
    check("renew without --ttl leaves the step to the vault (the token's own lifetime)", r.returncode == 0
          and vault_calls(td)[n:][-1][:3] == ["secrets", "token", "renew"] and "--ttl" not in vault_calls(td)[n:][-1],
          vault_calls(td)[n:])
    set_mode(td, "norenew")
    r = mc(td, "renew")
    check("renew with a vault from before `token renew`: says so (255), with the restart that works instead",
          r.returncode == 255 and "can't renew tokens" in r.stderr and "--continue" in r.stderr, r.stderr)
    set_mode(td, "soon")
    r = mc(td, "send", "are you there")
    check("send while the token has 2 minutes left: a warning naming `metacomputer claude renew`",
          r.returncode == 0 and "ends in 2 min" in r.stderr and "metacomputer claude renew" in r.stderr, r.stderr)
    r = mc(td, "status", "--session", "two", "--json")
    check("status --json carries the same warning (with --session)", "claude renew --session two" in
          json.dumps(js(r).get("warnings")), r.stdout)
    set_mode(td, "apikey")

    # --- the token never leaks ---
    ps = subprocess.run(["ps", "-axww", "-o", "args"], capture_output=True, text=True).stdout
    leaks.append(("ps on this Mac (and the stand-in VM)", ps))
    leaks.append(("tmux's arguments", open(os.path.join(td, "tmux-argv.log")).read()))
    hits = files_with(td, TOKEN, skip=(os.path.join(td, "vault"), os.path.join(td, "home", ".local", "state")))
    check("the token is in no file but the fake vault's (action log with ACTION_LOG_TEXT=keep, state, configs, "
          "tmux's log)", hits == [] or hits == [], hits)
    actions = list(every_file(os.path.join(td, "actions"))) if os.path.isdir(os.path.join(td, "actions")) else []
    alog = "".join(open(p, errors="replace").read() for p in actions)
    check("the action log has the start (tool vm_claude: secret, token id, auth) and no token",
          '"vm_claude"' in alog and "claude-sub" in alog and '"auth":"oauth"' in alog.replace(" ", "") and TOKEN not in alog,
          alog[:600])
    for where, text in leaks:
        check(f"no token in {where}", TOKEN not in text)

    # --- stop ---
    r = mc(td, "stop")
    check("stop: the session ends; the token's id and the command to revoke it are printed (not revoked by itself)",
          r.returncode == 0 and "stopped" in r.stdout and "metacomputer secrets revoke a" in r.stdout
          and not os.path.exists(os.path.join(td, "vault", "revoked")), r.stdout + r.stderr)
    r = mc(td, "read")
    check("read after stop: exit 6", r.returncode == 6, r.stdout + r.stderr)
    n = len(vault_calls(td))
    r = mc(td, "renew")
    check("renew after stop: exit 6 (no record of its token), and the vault isn't asked", r.returncode == 6
          and len(vault_calls(td)) == n, r.stderr)
    tid2 = json.load(open(os.path.join(td, "run", "claude", "two.json")))["token_id"]
    r = mc(td, "stop", "--session", "two", "--revoke")
    check("stop --revoke: the vault revokes that session's token", r.returncode == 0 and "revoked" in r.stdout
          and open(os.path.join(td, "vault", "revoked")).read().split() == [tid2]
          and not os.path.exists(os.path.join(td, "run", "claude", "two.json")), r.stdout + r.stderr)

    # --- failures ---
    set_mode(td, "oauth")
    open(os.path.join(home, "fake-claude-mode"), "w").write("die")
    r = mc(td, "--session", "dies")
    revoked = open(os.path.join(td, "vault", "revoked")).read().split()
    check("Claude Code ending at once: exit 6, its token revoked, its last screen shown (no raw escapes)",
          r.returncode == 6 and len(revoked) == 2 and "Invalid API key" in r.stderr and "Please run /login" in r.stderr
          and "Pane is dead" in r.stderr and "\x1b" not in r.stderr, (r.stderr, revoked))
    os.remove(os.path.join(home, "fake-claude-mode"))
    st_dies = os.path.join(home, ".local", "state", "metacomputer", "claude", "dies")
    check("... and its token's file was removed", not os.path.exists(os.path.join(st_dies, "env")))


def refusal_checks(td):
    home = setup(td)
    for mode, code, words, what in (
            ("locked", 3, "secrets unlock", "a locked vault: exit 3, and what the user runs"),
            ("other", 125, "no api secret for api.anthropic.com", "no usable secret: refused, with how to store one"),
            ("both", 2, "--token", "two candidates: name one"),
            ("unbound", 125, "secrets bind", "a VM the vault isn't bound to"),
            ("refuse", 125, "Touch ID", "the vault's refusal passed on as a refusal"),
            ("badheader", 125, "revoked", "a token whose header isn't Bearer or x-api-key: refused and revoked")):
        set_mode(td, mode)
        n = len(vault_calls(td))
        r = mc(td, "--session", "ref")
        check(f"start, {what}", r.returncode == code and words in r.stderr, (r.returncode, r.stderr[-400:]))
        if mode in ("locked", "other", "both", "unbound"):
            check(f"  ... and no token was asked for ({mode})", not any(c[1] == "token" for c in vault_calls(td)[n:]))
    set_mode(td, "oauth")
    n = len(vault_calls(td))
    r = mc(td, "--session", "ref", MCOMP_VM_SHELL="off")
    check("VM_SHELL=off: refused (125) before any token", r.returncode == 125 and "VM_SHELL=off" in r.stderr
          and len(vault_calls(td)) == n, r.stderr)
    r = mc(td, "--session", "ref", MCOMP_VM_SHELL="auto", MCOMP_DRIVER_MODE="bounded")
    check("bounded driver (VM_SHELL=auto): refused (125)", r.returncode == 125 and "bounded" in r.stderr, r.stderr)
    r = mc(td, "--session", "ref", MCOMP_VM_STATE="off")
    check("the VM off: exit 255, nothing asked", r.returncode == 255 and "off" in r.stderr and len(vault_calls(td)) == n, r.stderr)

    r = mc(td, "--session", "rv")
    set_mode(td, "norevoke")
    r2 = mc(td, "stop", "--session", "rv", "--revoke", "--json")
    d = js(r2)
    check("stop --revoke when the vault can't confirm the revocation: says so, with the command to end it",
          r.returncode == 0 and r2.returncode == 0 and d.get("token_revoked") is False
          and "metacomputer secrets revoke a" in d.get("message", ""), (r.stderr[-300:], r2.stdout))
    set_mode(td, "oauth")
    n = len(vault_calls(td))

    run_dir = os.path.join(td, "run", "owners")
    os.makedirs(run_dir, exist_ok=True)
    other = subprocess.Popen(["sleep", "60"])
    claim = os.path.join(run_dir, f"{other.pid}.json")
    try:
        now = time.time()
        json.dump({"bridge_pid": other.pid, "claude_pid": None, "cwd": "/another/project", "started": now,
                   "last_call": now, "last_tool": "click"}, open(claim, "w"))
        r = mc(td, "--session", "ref")
        check("another session acting: start refused (125) before any token", r.returncode == 125
              and "/another/project" in r.stderr and len(vault_calls(td)) == n, r.stderr)
        r = mc(td, "send", "hi", "--session", "ref")
        check("another session acting: send refused (125)", r.returncode == 125 and "/another/project" in r.stderr, r.stderr)
        r = mc(td, "status", "--session", "ref")
        check("... while status (read-only) still answers", r.returncode == 0, r.stderr)
    finally:
        other.kill()
        other.wait()
        if os.path.exists(claim):
            os.remove(claim)


def install_checks(td):
    home = setup(td, with_claude=False)
    set_mode(td, "oauth")
    r = mc(td, "--session", "inst", "--no-install")
    check("--no-install with no Claude Code in the VM: stops before the vault is asked", r.returncode == 255
          and "--no-install" in r.stderr and not vault_calls(td), r.stderr)
    r = mc(td, "--session", "inst")
    check("no Claude Code in the VM: Anthropic's installer is downloaded to a file, run, and Claude Code starts",
          r.returncode == 0 and os.path.exists(os.path.join(home, ".local", "bin", "claude"))
          and "https://claude.ai/install.sh" in open(os.path.join(home, "curl.log")).read()
          and claude_rec(td).get("oauth_sha"), (r.stdout, r.stderr))
    mc(td, "stop", "--session", "inst", "--revoke")
    r = mc(td, "--session", "inst")
    check("a second start: no second install, the PATH line still once", r.returncode == 0
          and open(os.path.join(home, "curl.log")).read().count("https://claude.ai/install.sh") == 1
          and open(os.path.join(home, ".bashrc")).read().count("# metacomputer: Claude Code") == 1, r.stderr)
    mc(td, "stop", "--session", "inst", "--revoke")


ENV_PROBE = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import vm_shell
o = {"cwd": "/", "timeout": 30, "max_output": 65536, "read_only": True, "override": False, "args": [],
     "source": "command"}
out = {}
for name, env in (("good", {"MCOMP_PROBE": "a value with spaces; $(x)"}), ("lower", {"bad_name": "x"}),
                  ("nul", {"MCOMP_PROBE": "a\u0000b"}), ("many", {"V%d" % i: "x" for i in range(17)}),
                  ("type", {"MCOMP_PROBE": 5})):
    res, code = vm_shell.run_guarded(o, ["bash", "-c"], b'printf %s "${MCOMP_PROBE:-unset}"', tool="probe",
                                     header_extra={"env": env})
    st = (res.get("_streams") or {}).get("stdout")
    out[name] = [code, res.get("status"), res.get("kind"), st["head"].decode() if st else None]
print(json.dumps(out))
'''


def guest_checks(td):
    """The VM's half on its own: arguments it refuses, and a token it's handed is gone from the environment."""
    open(os.path.join(td, "probe.py"), "w").write(ENV_PROBE)
    os.makedirs(os.path.join(td, "run"), exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(td, "probe.py"), os.path.join(LAB, "lib")], capture_output=True,
                       text=True, timeout=120, env=env_for(td))
    try:
        got = json.loads(r.stdout)
    except ValueError:
        got = {}
    check("the shell channel's header env: the script gets the value in its environment, never on its command line",
          got.get("good") == [0, "exited", None, "a value with spaces; $(x)"], (got, r.stderr[-300:]))
    check("... and the VM's half refuses a lowercase name, a NUL, more than 16 values or a value that isn't text",
          all((got.get(k) or [None, None, None])[2] == "guest" for k in ("lower", "nul", "many", "type")), got)
    env = dict(os.environ, HOME=td)
    r = subprocess.run(["bash", GUEST, "status", "Bad Name"], capture_output=True, text=True, env=env)
    check("guest: a bad session name is refused (2)", r.returncode == 2, r.stderr)
    r = subprocess.run(["bash", GUEST, "nonsense", "x"], capture_output=True, text=True, env=dict(env, PATH="/usr/bin:/bin"))
    check("guest: an unknown action is refused (2)", r.returncode == 2, r.stderr)
    text = open(GUEST).read()
    check("guest: the token and the prompt are taken out of the environment before anything runs",
          text.index("unset MCOMP_CLAUDE_TOKEN MCOMP_CLAUDE_TEXT") < text.index("find_tmux() {"))
    check("guest: the token is written only with printf (a builtin) into a 0077 file, never put on a command line",
          re.search(r'printf .export %s=%q\\n. "\$VAR" "\$TOKEN"', text) is not None
          and text.count("$TOKEN") == 3, [m.start() for m in re.finditer(r"\$TOKEN", text)])


def main():
    unit_checks()
    with tempfile.TemporaryDirectory(prefix="mcc-", dir="/tmp") as td:
        guest_checks(td)
    if tmux_real():
        for fn in (session_checks, refusal_checks, install_checks):
            td = tempfile.mkdtemp(prefix="mcc-", dir="/tmp")
            try:
                fn(td)
            finally:
                subprocess.run([tmux_real(), "-L", "metacomputer-claude", "kill-server"], capture_output=True,
                               env=dict(os.environ, TMUX_TMPDIR=os.path.join(td, "tmux")))
                shutil.rmtree(td, ignore_errors=True)
    else:
        skip("sessions, refusals and installs", "no tmux on this Mac (brew install tmux)")
    print(f"\n{sum(results)}/{len(results)} passed" + (f", {len(skipped)} groups skipped" if skipped else ""))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
