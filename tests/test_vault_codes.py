#!/usr/bin/env python3
"""Offline checks for one-time sign-in codes and links (`secrets ask | give | code | relay`, `fill --request`, the code and
relay rules) and human-mode fills (`fill --human`, `code --human`).

No VM needed; the approach of tests/test_secrets.py and tests/test_vault_approvals.py: a TEST build of mcomp-vault, an ssh
stub standing in for the VM (its own here, answering the fill account's read, open and fill requests and the desktop
account's human-mode script), every passphrase typed into a pseudo-terminal, every value a dummy. The dialog is never shown:
a stand-in for osascript (MCOMP_VAULT_TEST_OSASCRIPT, test builds only) records what it was given and answers.

Also checked without a VM: the human-mode script (vault/Human.swift) against a fake accessibility tree (a page whose title
names another site, a non-password field focused for a password, an address typed but not opened, a field in a frame),
secret-fill's read and open modes' pure parts, the dialog's AppleScript compiled by osacompile (compiled, never run), and
the wrapper's routing (bin/metacomputer).

Run: python3 tests/test_vault_codes.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build [MCOMP_VAULT_REL_BIN=/path/to/release-build] python3 tests/test_vault_codes.py
"""
import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402
import test_vault_ops as tvo  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
PW_VALUE = ts.PW_VALUE
USERNAME = ts.USERNAME
SITE = ts.SITE                      # http://127.0.0.1:8765
SITE2 = ts.SITE2                    # http://127.0.0.1:8766
MAIL = "http://localhost:8000"      # the page a relay reads from
IDP = "https://accounts.google.com"
CODE = "482913"
CODE2 = "771204"
CODE3 = "593017"
LINK = SITE + "/verify?t=dummy-link-token-5521"
LINK_TRACKED = "https://links.example.net/c/dummy-tracked-token-8812"
DIALOG_CODE = "DLG7731Q"
VALUES = [PASS, PW_VALUE, CODE, CODE2, CODE3, "dummy-link-token-5521", "dummy-tracked-token-8812", DIALOG_CODE]
REQ_RE = re.compile(r"Request ([0-9a-f]{8}):")
outputs = []

STUB = r'''#!{python}
# Stands in for /usr/bin/ssh in a TEST build of mcomp-vault: the fill account's secret-fill (check, fill, read, open)
# and the desktop account's human-mode script. Records what it got; answers like the VM.
import json, os, sys
LOG = {log!r}
d = os.path.dirname(LOG)
argv = sys.argv[1:]
remote = argv[-1] if argv else ""
dest = next((a for a in argv if a.endswith("@127.0.0.1")), "")
user = dest.split("@")[0]
def flag(n):
    p = os.path.join(d, n)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False
def val(n, default=None):
    p = os.path.join(d, n)
    return open(p).read() if os.path.exists(p) else default
raw = sys.stdin.buffer.read()
rec = {{"argv": argv, "user": user, "stdin": raw.decode("utf-8", "replace")}}
try:
    req = json.loads(raw)
except ValueError:
    req = {{}}
rec["mode"] = req.get("mode")
rec["human"] = "mcomp-human-fill" in remote
with open(LOG, "a") as f:
    f.write(json.dumps(rec) + "\n")
v = req.get("value") or ""
if rec["human"]:
    origin = val("human-origin", (req.get("origins") or ["?"])[0])
    if flag("human-deny-next"):
        out = {{"ok": False, "denied": True, "reason": val("human-deny-reason", "the focused element is a entry, not a password field")}}
    elif flag("human-echo-next"):
        out = {{"ok": False, "denied": True, "reason": "the focused element is " + v}}
    elif origin not in (req.get("origins") or []):
        out = {{"ok": False, "denied": True, "reason": "the human-mode site is " + origin + ", which isn't allowed here"}}
    elif req.get("mode") == "check":
        out = {{"ok": True, "mode": "check", "field": req.get("field"), "origin": origin}}
    else:
        out = {{"ok": True, "mode": "fill", "field": req.get("field"), "origin": origin, "chars": len(v), "route": "keyboard"}}
elif user == "mcfill":
    mode = req.get("mode")
    origin = val("tab-origin", (req.get("origins") or ["?"])[0])
    if mode == "read":
        if origin not in (req.get("origins") or []):
            out = {{"ok": False, "denied": True, "reason": "the tab is on " + origin + ", which isn't allowed here"}}
        elif req.get("what") == "links":
            out = {{"ok": True, "mode": "read", "origin": origin, "links": json.loads(val("read-links", "[]"))}}
        else:
            out = {{"ok": True, "mode": "read", "origin": origin, "text": val("read-text", "")}}
    elif flag("deny-next"):
        out = {{"ok": False, "denied": True, "reason": "no field is focused in the page; click into the field first"}}
    elif flag("echo-next"):
        out = {{"ok": False, "denied": False, "reason": "saw " + v}}
    elif origin not in (req.get("origins") or []):
        out = {{"ok": True, "mode": mode, "field": req.get("field"), "origin": origin}}
    elif mode == "check":
        out = {{"ok": True, "mode": "check", "field": req.get("field"), "origin": origin}}
    elif mode == "open":
        out = {{"ok": True, "mode": "open", "field": "text", "origin": origin}}
    else:
        out = {{"ok": True, "mode": "fill", "field": req.get("field"), "origin": origin, "chars": len(v), "route": "element"}}
else:
    out = {{}}
print(json.dumps(out))
'''

OSA_STUB = r'''#!{python}
# Stands in for osascript in a TEST build: records its arguments (the script lines and the words passed in), then answers
# with what the test left in "answer" (nothing: the person cancelled).
import json, os, sys
d = {dir!r}
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = os.path.join(d, "answer")
if os.path.exists(a):
    print(open(a).read().strip())
else:
    sys.exit(1)
'''


def wait_for(fn, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def main():
    td = tempfile.mkdtemp(prefix="mcvc-", dir="/tmp")
    procs = []
    try:
        offline_checks(td)
        run(td, procs)
    finally:
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
            if isinstance(p, ts.Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if ts.results and all(ts.results) else 1


# --- the human-mode script, against a fake accessibility tree ---------------------------------------------------
def human_module():
    src = open(os.path.join(LAB, "vault", "Human.swift")).read()
    m = re.search(r'let humanFillScript = #"""\n(.*?)\n"""#', src, re.S)
    mod = types.ModuleType("mcomp_human_fill")
    exec(compile(m.group(1), "humanFillScript", "exec"), mod.__dict__)
    return mod


class FakeTree:
    """Nodes: (role, name, states, text, docurl, children)."""

    def __init__(self, app_name, frames, pid=4242):
        self.pid, self.nodes, self.counts, self.geo = pid, {}, {}, {}
        self.app = self._add(("application", app_name, 0, "", None, frames))

    def _add(self, n):
        ref = ("fake", "/n%d" % len(self.nodes))
        role, name, st, text, url, kids = n
        self.nodes[ref] = None                    # its place first, so its children get refs of their own
        self.nodes[ref] = (role, name, st, text, url, [self._add(k) for k in kids])
        return ref

    def apps(self):
        return [(self.app, self.pid)]

    def role(self, r):
        return self.nodes[r][0]

    def name(self, r):
        return self.nodes[r][1]

    def states(self, r):
        return self.nodes[r][2]

    def children(self, r):
        return self.nodes[r][5]

    def text(self, r):
        return self.nodes[r][3]

    def char_count(self, r):
        return self.counts.get(r, len(self.nodes[r][3]))

    def doc_url(self, r):
        return self.nodes[r][4]

    def extents(self, r):
        return self.geo.get(r)

    def find(self, role, name=None):
        return next(r for r, n in self.nodes.items() if n[0] == role and (name is None or n[1] == name))


def S(*bits):
    v = 0
    for b in bits:
        v |= 1 << b
    return v


def firefox(bar_text, doc_url, title, fields, bar_states=()):
    """A Firefox window: the toolbar's address bar and the page's document with its fields."""
    H = human_module()
    show = H.SHOWING
    bar = ("combo box", "Search with Google or enter address", S(show, H.EDITABLE, *bar_states), bar_text, None, [])
    nav = ("tool bar", "Navigation", S(show), "", None, [("panel", "", S(show), "", None, [bar])])
    doc = ("document web", title, S(show), "", doc_url, fields)
    content = ("internal frame", "", S(show), "", None, [doc])
    return ("frame", title + " — Mozilla Firefox", S(show, H.ACTIVE), "", None, [nav, ("scroll pane", "", S(show), "", None, [content])])


def offline_checks(td):
    H = human_module()
    show, ed, foc, ro = H.SHOWING, H.EDITABLE, H.FOCUSED, H.READ_ONLY
    # bar_origin: the address bar against the page's own address
    check("human mode: an http address bar names its site with the scheme (it must equal the page's address)",
          H.bar_origin("http://localhost:8000/site.html", "http://localhost:8000/site.html") == "http://localhost:8000")
    check("human mode: Firefox's trimmed bar for a secure page (no https://) counts only when the page's address is https",
          H.bar_origin("example.com", "https://example.com/") == "https://example.com")
    for bar, url, what in [("example.com", "http://example.com/", "the page isn't https"),
                           ("bank.example", "https://evil.example/", "an address typed but not opened"),
                           ("https://bank.example/login", "https://evil.example/login", "a full address that isn't the page's"),
                           ("bank example", "https://bank.example/", "a search typed into the bar"),
                           ("", "https://bank.example/", "an empty bar")]:
        try:
            H.bar_origin(bar, url)
            ok = False
        except H.Refused:
            ok = True
        check(f"human mode: refused when {what}", ok)

    def inspect(tree, field, origins):
        try:
            return H.inspect(tree, tree.pid, None, field, origins)
        except H.Refused as e:
            return "refused: " + str(e)

    code_f = ("entry", "Code", S(show, ed, foc), "", None, [])
    pw_f = ("password text", "Password", S(show, ed), "", None, [])
    # the misleading title: the window, tab and heading name https://bank.example; the address bar names the real site
    t = FakeTree("Firefox", [firefox("http://localhost:8000/misleading.html", "http://localhost:8000/misleading.html",
                                     "https://bank.example/login - Bank sign-in", [code_f, pw_f])])
    r = inspect(t, "otp", ["https://bank.example"])
    check("human mode: a page whose title (and heading) name another site is refused for that site: the site comes from "
          "the address bar", isinstance(r, str) and "http://localhost:8000" in r and "isn't allowed" in r, r)
    r = inspect(t, "otp", ["http://localhost:8000"])
    check("human mode: ...and the same field is accepted for the site the address bar names", isinstance(r, tuple) and r[0] == "http://localhost:8000", r)
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                     [code_f, pw_f])])
    r = inspect(t, "password", ["http://localhost:8000"])
    check("human mode: a focused non-password field is refused for --field password", isinstance(r, str) and "not a password field" in r, r)
    r = inspect(t, "otp", ["http://localhost:8001"])
    check("human mode: the origin mismatch is refused (port differs)", isinstance(r, str) and "isn't allowed" in r, r)
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                     [("entry", "Code", S(show, ed), "", None, []), ("password text", "Password", S(show, ed, foc), "", None, [])])])
    r = inspect(t, "password", ["http://localhost:8000"])
    check("human mode: a focused password text field is accepted for --field password", isinstance(r, tuple), r)
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                     [("entry", "Code", S(show, ed, foc, ro), "", None, [])])])
    check("human mode: a read-only field is refused", "read-only" in str(inspect(t, "otp", ["http://localhost:8000"])))
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                     [("entry", "Code", S(show, ed), "", None, [])], bar_states=(foc,))])
    check("human mode: the address bar itself focused (being edited) is refused", "browser's own controls" in str(inspect(t, "otp", ["http://localhost:8000"])))
    inner = ("internal frame", "", S(show), "", None, [("document web", "ad", S(show), "", "https://evil.example/", [
        ("entry", "Code", S(show, ed, foc), "", None, [])])])
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example", [inner])])
    check("human mode: a field inside a frame (whose site the bar doesn't show) is refused", "inside a frame" in str(inspect(t, "otp", ["http://localhost:8000"])))
    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                     [("entry", "Code", S(show, ed), "", None, [])])])
    check("human mode: nothing focused in the page is refused", "no field is focused" in str(inspect(t, "otp", ["http://localhost:8000"])))
    two = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example",
                                       [("entry", "Code", S(show, ed), "", None, [])]),
                               firefox("http://localhost:8001/site.html", "http://localhost:8001/site.html", "Example", [code_f])])
    f1, f2 = [r for r, n in two.nodes.items() if n and n[0] == "frame"]
    two.geo = {f1: (5, 56, 1280, 947), f2: (635, 56, 1280, 947)}
    r = H.inspect(two, two.pid, "Example — Mozilla Firefox", "otp", ["http://localhost:8001"], (635, 56, 1280, 947))
    check("human mode: of two browser windows with the same title, the one at the X window's place and size is checked",
          isinstance(r, tuple) and r[0] == "http://localhost:8001", r)
    try:
        H.inspect(two, two.pid, "Example — Mozilla Firefox", "otp", ["http://localhost:8000"], (5, 56, 1280, 947))
        r = "accepted"
    except H.Refused as e:
        r = str(e)
    check("human mode: ...and the other one (nothing focused there) is refused, not mixed up with it", "no field is focused" in r, r)
    t = FakeTree("Mousepad", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example", [code_f])])
    check("human mode: a window that isn't a browser is refused", "isn't a browser" in str(inspect(t, "otp", ["http://localhost:8000"])))

    class FakeDriver:
        def __init__(self, tree, ref, value):
            self.calls, self.tree, self.ref, self.value = [], tree, ref, value

        def call(self, method, params, timeout=20):
            self.calls.append((method, params))
            if params.get("name") == "type_text":
                self.tree.counts[self.ref] = len(params["arguments"]["text"])
            return {}

        def close(self):
            pass

    t = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example", [code_f])])
    drv = FakeDriver(t, t.find("entry", "Code"), CODE)
    out = H.run({"mode": "fill", "field": "otp", "origins": ["http://localhost:8000"], "value": CODE}, tree=t, env={},
                info=(77, t.pid, None, None), driver=drv)
    typed = [p for m, p in drv.calls if p.get("name") == "type_text"]
    check("human mode: the value is typed through the driver as foreground keys to that window, then counted in the field",
          out.get("ok") is True and out.get("chars") == len(CODE) and out.get("route") == "keyboard" and len(typed) == 1
          and typed[0]["arguments"].get("delivery_mode") == "foreground" and typed[0]["arguments"].get("window_id") == 77
          and CODE not in json.dumps(out), (out, drv.calls))
    t2 = FakeTree("Firefox", [firefox("http://localhost:8000/site.html", "http://localhost:8000/site.html", "Example", [code_f])])
    drv2 = FakeDriver(t2, ("none", "none"), CODE)
    out = H.run({"mode": "fill", "field": "otp", "origins": ["http://localhost:8000"], "value": CODE}, tree=t2, env={},
                info=(77, t2.pid, None, None), driver=drv2)
    check("human mode: a field that doesn't hold as many characters afterwards is reported (the VM's count, never the text)",
          out.get("ok") is False and "the field holds" in out.get("reason", "") and CODE not in json.dumps(out), out)

    # secret-fill's read: the script it runs in the isolated world, and the link check
    sf = ts.__dict__.get("load_secret_fill")
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("secret_fill_mod", os.path.join(LAB, "provisioning/guest/rootfs/opt/metacomputer/bin/secret-fill"))
    spec = importlib.util.spec_from_loader("secret_fill_mod", loader)
    sfm = importlib.util.module_from_spec(spec)
    loader.exec_module(sfm)
    js = sfm.read_js('#code"); alert(1); ("', "text")
    check("secret-fill read: the selector goes into its script as a JSON string (no way out of it), the text is bounded",
          json.dumps('#code"); alert(1); ("') in js and str(sfm.READ_MAX) in js and "querySelectorAll(sel)" in js)
    for req, want in [({"mode": "open", "field": "text", "origins": [SITE], "value": "https://evil.example/x", "link_origin": SITE}, "the link isn't one"),
                      ({"mode": "open", "field": "text", "origins": [SITE], "value": "javascript:alert(1)", "link_origin": SITE}, "the link isn't one"),
                      ({"mode": "read", "field": "text", "origins": [SITE], "what": "html", "selector": "#x"}, "bad request")]:
        try:
            sfm.run(req, {"uid": 1000, "proc_net": [], "proc_root": "/proc"})
            got = "ran"
        except (sfm.Denied, sfm.Failed) as e:
            got = str(e)
        check(f"secret-fill {req['mode']}: refused before any tab is looked at ({want})", want in got, got)

    # the dialog's AppleScript compiles (osacompile: compiled, never run, so no dialog appears)
    src = open(os.path.join(LAB, "vault", "Codes.swift")).read()
    body = re.search(r"let codeDialogScript: \[String\] = \[\n(.*?)\n\]", src, re.S).group(1)
    lines = [json.loads(l.strip().rstrip(",")) for l in body.splitlines() if l.strip()]
    args = ["/usr/bin/osacompile", "-o", os.path.join(td, "dialog.scpt")]
    for l in lines:
        args += ["-e", l]
    r = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    check("the dialog's AppleScript compiles (osacompile only; nothing is shown)", r.returncode == 0, r.stderr[-300:])
    check("the dialog's script names no request words: they come in as `on run argv` items",
          lines[0] == "on run argv" and all("argv" in l or "theText" in l or l.startswith(("set r", "if gave", "return", "end run", "on run")) or "item" in l
                                            for l in lines) and not any(c in "".join(lines) for c in ("%s", "\\(")))

    # the wrapper: INTERACTIVE_PROMPTS reaches the unlock, the new commands go through the handoff check
    w = open(os.path.join(LAB, "bin", "metacomputer")).read()
    check("bin/metacomputer passes INTERACTIVE_PROMPTS to the vault (read at unlock), off when unset",
          'export MCOMP_INTERACTIVE_PROMPTS="${INTERACTIVE_PROMPTS:-off}"' in w)
    check("bin/metacomputer: code, relay and give wait for an open handoff like fills (handoff_hold)",
          re.search(r'\[ "\$sub" = code \] \|\| \[ "\$sub" = relay \] \|\| \[ "\$sub" = give \]', w) is not None
          and "fill|login|session|token|code|relay|give)" in w)
    check("bin/metacomputer: approving a waiting code request (which then fills) waits for an open handoff too",
          re.search(r'approve\)\n.*?handoff_hold\n.*?exec "\$VAULT_BIN" approve', w, re.S) is not None)
    d = open(os.path.join(LAB, "config", "defaults.env")).read()
    check("config/defaults.env: INTERACTIVE_PROMPTS=off and CODES_VIA=vault by default",
          re.search(r"^INTERACTIVE_PROMPTS=off\b", d, re.M) and re.search(r"^CODES_VIA=vault\b", d, re.M))


# --- the vault, end to end, against the stub ----------------------------------------------------------------------
def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    REL = os.environ.get("MCOMP_VAULT_REL_BIN")
    if not REL and not os.environ.get("MCOMP_VAULT_SKIP_RELEASE"):
        REL = os.path.join(td, "bin", "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--out", REL], capture_output=True, text=True)
        check("build: release binary", r.returncode == 0, r.stderr[-800:])
    if REL:
        blob = open(REL, "rb").read()
        check("the release build has no dialog stand-in or short-TTL switch (MCOMP_VAULT_TEST_OSASCRIPT, _CODE_MIN_TTL) and shows "
              "dialogs only through /usr/bin/osascript", b"MCOMP_VAULT_TEST_OSASCRIPT" not in blob and b"MCOMP_VAULT_TEST_CODE_MIN_TTL" not in blob
              and b"/usr/bin/osascript" in blob)
        r = subprocess.run([REL, "code", "--vm", "x", "--site", SITE, "--into", SITE + "/#otp"], capture_output=True, text=True,
                           input=CODE + "\n", env={"PATH": "/usr/bin:/bin", "HOME": td, "MCOMP_VAULT_DIR": os.path.join(td, "nov")})
        check("the release build's `code` with no unlock: locked (exit 3), and no value in its output",
              r.returncode == 3 and CODE not in r.stdout + r.stderr, (r.returncode, r.stdout[-200:], r.stderr[-200:]))
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []

    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    osadir = os.path.join(td, "osa")
    os.makedirs(osadir)
    osa = os.path.join(osadir, "osascript")
    open(osa, "w").write(OSA_STUB.format(python=sys.executable, dir=osadir))
    os.chmod(osa, 0o755)
    spool = os.path.join(td, "spool")
    os.makedirs(spool, mode=0o700)
    fake = os.path.join(td, "fake", "claude")
    os.makedirs(os.path.dirname(fake))
    open(fake + ".c", "w").write(tvo.FAKE_CLAUDE_C)
    r = subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True, text=True)
    check("a stand-in process named claude builds", r.returncode == 0, r.stderr)
    proj = os.path.realpath(os.path.join(td, "proj"))
    os.makedirs(proj)

    def sflag(name, content=""):
        open(os.path.join(stubdir, name), "w").write(content)

    def sunflag(name):
        p = os.path.join(stubdir, name)
        if os.path.exists(p):
            os.remove(p)

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_SSH": stub, "MCOMP_EVENTS_SPOOL": spool, "MCOMP_VAULT_TEST_CODE_MIN_TTL": "2",
           "MCOMP_VAULT_TEST_LISTENERS": f"{my_name},cua-netgw", "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "20"}
    alog = os.path.join(vdir, "audit.jsonl")

    def client(*args, extra=None, timeout=120, stdin=None):
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=dict(env, **(extra or {})),
                           input=stdin if stdin is not None else "", timeout=timeout)
        outputs.append(r.stdout + r.stderr)
        return r

    def js(text):
        try:
            return json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError, AttributeError):
            return {}

    def tty(argv, answers, extra=None, timeout=90):
        p = ts.Pty(argv, dict(env, **(extra or {})))
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        outputs.append(p.text())
        return rc, p.text()

    def start_unlock(extra=None):
        p = ts.Pty([BIN, "unlock", "--for", "30m", *allow], dict(env, **(extra or {})))
        procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        wait_for(lambda: os.path.exists(os.path.join(vdir, "vault.sock")), 5, 0.05)
        return p

    def lock(p):
        p.send("\x03")
        try:
            return p.wait(15)
        finally:
            outputs.append(p.text())

    def audit_recs():
        return [json.loads(l) for l in open(alog)] if os.path.exists(alog) else []

    def events():
        out = []
        for f in sorted(glob.glob(os.path.join(spool, "*.json"))):
            try:
                out.append(json.load(open(f)))
            except ValueError:
                pass
        return out

    def state(rid):
        r = client("requests", "--json")
        return next((q for q in json.loads(r.stdout or "{}").get("requests", []) if q.get("id") == rid), None)

    # --- the vault ------------------------------------------------------------------------------------------------
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("init", rc == 0, out)
    rc, out = tty([BIN, "set", "web", "--kind", "password", "--origin", SITE, "--username", USERNAME],
                  [("Vault passphrase", PASS + "\n"), ("Password for web", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set a login (session approval) for the stand-in site", rc == 0, out)
    rc, out = tty([BIN, "bind", "cvm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill",
                   "--fill-user", "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe],
                  [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind the stand-in VM", rc == 0, out)

    # --- 1. stdin only, never argv or the environment ----------------------------------------------------------
    r = client("give", "0badc0de", CODE)
    check("give: a value on the command line is refused (usage, exit 64) and never echoed", r.returncode == 64 and CODE not in r.stdout + r.stderr, (r.returncode, r.stderr[-200:]))
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", CODE)
    check("code: a value on the command line is refused (exit 64), never echoed", r.returncode == 64 and CODE not in r.stdout + r.stderr, r.stderr[-200:])
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", stdin=CODE + "\n")
    check("code while the vault is locked: exit 3 (locked), the value not in the output", r.returncode == 3 and CODE not in r.stdout + r.stderr, (r.returncode, r.stdout[-200:]))
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE2 + "/#otp", stdin=CODE + "\n")
    check("code: --into naming another site than --site is refused before anything is sent", r.returncode == 64 and "own site" in r.stderr, r.stderr[-300:])

    u = start_unlock()
    n0 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin="x" * 5000 + "\n")
    check("code: a value longer than 4096 bytes on stdin is refused, nothing sent to the VM", r.returncode == 2 and len(calls()) == n0, (r.returncode, r.stderr[-200:]))
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin="12\n")
    check("code: a value that doesn't look like a code (2 digits) is refused, nothing sent to the VM",
          r.returncode == 2 and js(r.stdout).get("status") == "denied" and len(calls()) == n0, r.stdout[-300:])

    # --- 2. by hand: ask, the event, requests, give from a pipe (no terminal) without a rule ------------------------
    r = client("ask", "--vm", "cvm", "--site", SITE, "--kind", "code", "--why", "X sign-in: the code sent by email", "--into", SITE + "/#otp", "--json")
    a = js(r.stdout)
    rid = a.get("request_id")
    check("ask returns the request id at once (exit 0, state pending), and how to give it",
          r.returncode == 0 and rid and a.get("state") == "pending" and a.get("give") == f"metacomputer secrets give {rid}", r.stdout[-300:])
    q = state(rid) or {}
    check("requests lists it: site, kind, why, pending", q.get("site") == SITE and q.get("kind") == "code" and q.get("state") == "pending"
          and "X sign-in" in q.get("why", ""), q)
    ev = [e for e in events() if e.get("type") == "code_requested"]
    check("an event (needs_you.code_requested) through the spool: site, kind, request id, why; never a value",
          len(ev) == 1 and ev[0].get("family") == "needs_you" and ev[0]["data"].get("request_id") == rid and ev[0]["data"].get("site") == SITE
          and ev[0]["data"].get("kind") == "code" and "X sign-in" in ev[0]["data"].get("why", ""), ev)
    check("without INTERACTIVE_PROMPTS=on no dialog is shown (the osascript stand-in wasn't run)", not os.path.exists(os.path.join(osadir, "calls.jsonl")))
    r = client("give", rid, "--json", stdin="482 913\n")
    g = js(r.stdout)
    check("give from a pipe (no terminal) with no rule: exit 7 (needs approval) at once, naming the one command",
          r.returncode == 7 and g.get("status") == "needs_approval" and g.get("approve") == f"metacomputer secrets approve {rid}"
          and CODE not in r.stdout + r.stderr, (r.returncode, r.stdout[-300:]))
    check("...nothing was filled yet", not [c for c in calls()[n0:] if c.get("mode") == "fill"])
    q = state(rid) or {}
    check("...requests shows it waiting for approval", q.get("needs_approval") is True and q.get("state") == "given", q)
    r = client("approve", rid, extra={})
    check("approve ID with no Touch ID stand-in in the unlock: not approved (a test build never shows the dialog)", r.returncode != 0, r.stdout[-200:] + r.stderr[-200:])
    lock(u)

    u = start_unlock(extra={"MCOMP_VAULT_TEST_LOCAL_AUTH": "allow"})
    check("locking drops every request: none is listed after the next unlock", state(rid) is None)
    n1 = len(calls())
    r = client("ask", "--vm", "cvm", "--site", SITE, "--kind", "code", "--why", "X code", "--into", SITE + "/#otp", "--json")
    rid = js(r.stdout).get("request_id")
    r = client("give", rid, "--json", stdin=CODE + "\n")
    check("give from a pipe again: needs approval", r.returncode == 7, r.stdout[-200:])
    r = client("approve", rid, "--json")
    ap = js(r.stdout)
    fills = [c for c in calls()[n1:] if c.get("mode") == "fill"]
    check("approve ID (Touch ID stand-in) fills it: the fill account got exactly the request's site and field, the value only on stdin",
          r.returncode == 0 and ap.get("ok") is True and len(fills) == 1 and json.loads(fills[0]["stdin"]).get("origins") == [SITE]
          and json.loads(fills[0]["stdin"]).get("value") == CODE and json.loads(fills[0]["stdin"]).get("selector") == "#otp"
          and not any(CODE in x for x in fills[0]["argv"]) and CODE not in r.stdout + r.stderr,
          (r.stdout[-300:], [(json.loads(f["stdin"]).get("origins"), json.loads(f["stdin"]).get("selector"), json.loads(f["stdin"]).get("value") == CODE,
                              [x for x in f["argv"] if CODE in x]) for f in fills]))
    r = client("give", rid, "--json", stdin=CODE2 + "\n")
    check("single use: a second give for a filled request is refused", r.returncode in (2, 6) and len([c for c in calls()[n1:] if c.get("mode") == "fill"]) == 1, r.stdout[-200:])
    recs = audit_recs()
    ops = [x.get("op") for x in recs if x.get("request_id") == rid]
    check("the audit log records the request, the give, the approval and the fill (no value)",
          {"code_request", "code_give", "request", "authorize", "code_fill"} <= set(ops), ops)

    # --- 3. expiry -------------------------------------------------------------------------------------------------
    r = client("ask", "--vm", "cvm", "--site", SITE, "--kind", "code", "--why", "short", "--ttl", "2s", "--json")
    rid2 = js(r.stdout).get("request_id")
    time.sleep(3.2)
    r = client("give", rid2, "--json", stdin=CODE + "\n")
    check("a request expires on its own: give after its time gives exit 6 (expired)", r.returncode == 6, (r.returncode, r.stdout[-200:]))
    check("...recorded as code_expire", any(x.get("op") == "code_expire" and x.get("request_id") == rid2 for x in audit_recs()))
    r = client("ask", "--vm", "cvm", "--site", SITE, "--kind", "code", "--why", "wait", "--ttl", "2s", "--wait", "10s", "--json")
    check("ask --wait returns when the request ends (expired: exit 6)", r.returncode == 6 and js(r.stdout).get("status") == "expired", r.stdout[-200:])

    # --- 4. rules: code --from stdin, expiry, identity providers ------------------------------------------------
    r = client("rule", "add", "code", "--site", IDP, "--from", "stdin")
    check("rule add code for an identity provider's site is refused: identity providers always ask", r.returncode != 0 and "identity provider" in r.stderr, r.stderr[-300:])
    r = client("rule", "add", "code", "--site", SITE, "--from", "stdin", "--expires", "40d")
    check("rule add code: --expires is at most 30 days", r.returncode == 64, r.stderr[-200:])
    r = client("rule", "add", "code", "--site", SITE, "--from", "pipe")
    check("rule add code: --from stdin is the only source", r.returncode == 64, r.stderr[-200:])
    r = client("rule", "add", "code", "--site", SITE, "--from", "stdin", "--expires", "4s")
    check("rule add code --site S --from stdin (Touch ID stand-in) is made, with its expiry", r.returncode == 0 and "Rule " in r.stdout, r.stdout + r.stderr)
    n2 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE2 + "\n")
    c = js(r.stdout)
    fills = [x for x in calls()[n2:] if x.get("mode") == "fill"]
    check("code from a pipe under the rule: filled at once, the script gets {status: filled} and never the value",
          r.returncode == 0 and c.get("status") == "filled" and c.get("ok") is True and CODE2 not in r.stdout + r.stderr
          and len(fills) == 1 and json.loads(fills[0]["stdin"]).get("value") == CODE2, (r.stdout[-300:], r.stderr[-200:]))
    check("...the audit record says the rule covered it", any(x.get("op") == "code_fill" and str(x.get("approval", "")).startswith("rule:") for x in audit_recs()))
    time.sleep(4.5)
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE3 + "\n")
    check("after the rule expires, the same command needs approval again (exit 7)", r.returncode == 7, r.stdout[-200:])
    rlist = client("rule", "list", "--json")
    check("rule list shows no expired rule", '"code"' not in rlist.stdout, rlist.stdout[-300:])
    r = client("rule", "add", "code", "--site", SITE2, "--from", "stdin", "--project", proj)
    check("rule add code --project limits it to Claude Code sessions in that folder", r.returncode == 0, r.stderr[-200:])
    p_ = subprocess.Popen([fake, proj, "/bin/sh", "-c", f"echo {CODE} | {shlex.quote(BIN)} code --vm cvm --site {SITE2} --into {SITE2}/#otp --json"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    o, e = p_.communicate(timeout=60)
    outputs.append(o + e)
    check("...a Claude Code session there is covered (filled)", p_.returncode == 0 and js(o).get("status") == "filled", o[-200:] + e[-200:])
    r = client("code", "--vm", "cvm", "--site", SITE2, "--into", SITE2 + "/#otp", "--json", stdin=CODE + "\n")
    check("...a program outside Claude Code isn't (needs approval)", r.returncode == 7, r.stdout[-200:])
    r = client("code", "--vm", "cvm", "--site", IDP, "--into", IDP + "/#otp", "--json", stdin=CODE + "\n")
    check("a code for an identity provider's site from a pipe always needs approval (no rule can cover it)", r.returncode == 7, r.stdout[-200:])

    # --- 5. site and host binding -----------------------------------------------------------------------------------
    r = client("rule", "add", "code", "--site", SITE, "--from", "stdin", "--expires", "1h")
    check("a fresh code rule for the site", r.returncode == 0, r.stderr[-200:])
    sflag("tab-origin", SITE2)
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE + "\n")
    sunflag("tab-origin")
    check("site binding: a VM that reports a tab on another site gets nothing kept as filled (refused)",
          r.returncode != 0 and js(r.stdout).get("status") != "filled", r.stdout[-300:])
    sflag("deny-next")
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE + "\n")
    d1 = js(r.stdout)
    check("a fill refused by a check (nothing typed) is refused (exit 2) and the request stays given for `fill --request`",
          r.returncode == 2 and d1.get("state") == "given", r.stdout[-300:])
    r = client("fill", "--request", d1.get("request_id", "00000000"), "--vm", "cvm")
    check("...fill --request ID then fills it", r.returncode == 0 and js(r.stdout).get("status") == "filled", r.stdout[-300:])
    sflag("echo-next")
    r = client("code", "--vm", "cvm", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE3 + "\n")
    check("a VM answer that holds the value is withheld (the code never reaches the output)",
          CODE3 not in r.stdout + r.stderr and r.returncode != 0, r.stdout[-300:])
    # links
    n3 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--kind", "link", "--into", SITE, "--json", stdin=LINK + "\n")
    opens = [x for x in calls()[n3:] if x.get("mode") == "open"]
    check("a sign-in link to the site's own host is opened in the tab on the site (the rule covers it), the link only on stdin",
          r.returncode == 0 and js(r.stdout).get("status") == "filled" and len(opens) == 1
          and json.loads(opens[0]["stdin"]).get("link_origin") == SITE and json.loads(opens[0]["stdin"]).get("origins") == [SITE]
          and "dummy-link-token-5521" not in r.stdout + r.stderr, (r.stdout[-300:], opens))
    n3 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--kind", "link", "--into", SITE, "--json", stdin=LINK_TRACKED + "\n")
    check("a link to another host (a tracking redirect) that the rule doesn't list is refused without a prompt, nothing opened",
          r.returncode == 2 and "links.example.net" in r.stdout and not [x for x in calls()[n3:] if x.get("mode") == "open"], r.stdout[-300:])
    r = client("code", "--vm", "cvm", "--site", SITE, "--kind", "link", "--into", SITE, "--json", stdin="http://evil.example/x\n")
    check("a link that isn't https (or local) is refused", r.returncode == 2, r.stdout[-200:])
    r = client("rule", "add", "code", "--site", SITE, "--from", "stdin", "--link-host", "links.example.net", "--expires", "1h")
    check("rule add code --link-host H", r.returncode == 0, r.stderr[-200:])
    n3 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--kind", "link", "--into", SITE, "--json", stdin=LINK_TRACKED + "\n")
    opens = [x for x in calls()[n3:] if x.get("mode") == "open"]
    check("...then the tracked link is opened (its own origin named to the VM)", r.returncode == 0 and len(opens) == 1
          and json.loads(opens[0]["stdin"]).get("link_origin") == "https://links.example.net", (r.stdout[-300:], opens))
    r = client("rule", "add", "code", "--site", SITE, "--from", "stdin", "--link-host", "accounts.google.com")
    check("a rule can't send links to an identity provider's host", r.returncode != 0 and "identity provider" in r.stderr, r.stderr[-200:])

    # --- 6. relay -------------------------------------------------------------------------------------------------------
    sflag("read-text", f"Your sign-in code is {CODE}. It expires in 10 minutes. Codes: none else.")
    n4 = len(calls())
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE, "--json")
    check("relay with no rule: exit 7 at once (needs approval), nothing filled",
          r.returncode == 7 and not [x for x in calls()[n4:] if x.get("mode") == "fill"] and CODE not in r.stdout + r.stderr, r.stdout[-300:])
    reads = [x for x in calls()[n4:] if x.get("mode") == "read"]
    check("...the source was read through the fill account, bound to the --from site and selector",
          len(reads) == 1 and json.loads(reads[0]["stdin"]).get("origins") == [MAIL] and json.loads(reads[0]["stdin"]).get("selector") == "#body", reads)
    r = client("rule", "add", "relay", "--from-host", "localhost", "--to", IDP)
    check("rule add relay into an identity provider is refused", r.returncode != 0 and "identity provider" in r.stderr, r.stderr[-200:])
    r = client("rule", "add", "relay", "--from-host", "localhost", "--to", SITE, "--expires", "1h")
    check("rule add relay --from-host H --to S", r.returncode == 0, r.stderr[-200:])
    n4 = len(calls())
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE)
    fills = [x for x in calls()[n4:] if x.get("mode") == "fill"]
    check("relay under the rule: Claude learns only 'relayed: filled'; the code went from the page to the field",
          r.returncode == 0 and r.stdout.strip().startswith("relayed: filled") and CODE not in r.stdout + r.stderr
          and len(fills) == 1 and json.loads(fills[0]["stdin"]).get("value") == CODE and json.loads(fills[0]["stdin"]).get("origins") == [SITE],
          (r.stdout[-200:], r.stderr[-200:]))
    sflag("read-text", f"Codes: {CODE} and {CODE2}")
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE, "--json")
    check("relay: an element with two code-like values is refused (failed, reason), nothing filled",
          r.returncode == 2 and "2 things that look like a code" in r.stdout and CODE not in r.stdout + r.stderr, r.stdout[-300:])
    sflag("read-text", "Hello there, nothing here")
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE, "--json")
    check("relay: an element with no code is refused", r.returncode == 2 and "nothing that looks like a code" in r.stdout, r.stdout[-300:])
    sflag("tab-origin", "http://localhost:9999")
    sflag("read-text", f"code {CODE}")
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE, "--json")
    sunflag("tab-origin")
    check("relay: a source tab on another site than --from is refused", r.returncode == 2, r.stdout[-300:])
    sflag("read-links", json.dumps([LINK]))
    n4 = len(calls())
    r = client("relay", "--vm", "cvm", "--kind", "link", "--from", MAIL + "/#signin-link", "--to", SITE, "--site", SITE, "--json")
    opens = [x for x in calls()[n4:] if x.get("mode") == "open"]
    check("relay --kind link: the site's link is opened in the tab on the site", r.returncode == 0 and len(opens) == 1
          and "dummy-link-token-5521" not in r.stdout + r.stderr, r.stdout[-300:])
    sflag("read-links", json.dumps(["https://tracker.example.org/r/1"]))
    r = client("relay", "--vm", "cvm", "--kind", "link", "--from", MAIL + "/#tracked-link", "--to", SITE, "--site", SITE, "--json")
    check("relay --kind link: a link to another host isn't opened without the person (refused under the rule)", r.returncode == 2, r.stdout[-300:])
    r = client("relay", "--vm", "cvm", "--from", MAIL + "/#body", "--to", SITE + "/#otp", "--site", SITE, "--human", "--json")
    check("relay into a human-mode browser isn't supported yet (refused, said)", r.returncode == 64 and "human-mode" in r.stderr, r.stderr[-200:])

    # --- 7. human mode -----------------------------------------------------------------------------------------------
    n5 = len(calls())
    r = client("fill", "web", "--vm", "cvm", "--field", "password", "--human")
    hs = [x for x in calls()[n5:] if x.get("human")]
    check("fill --human: checked first, then filled through the desktop account's human-mode script; the value only on stdin",
          r.returncode == 0 and js(r.stdout).get("mode") == "human" and [x["mode"] for x in hs] == ["check", "fill"]
          and hs[0]["user"] == "cua" and json.loads(hs[1]["stdin"]).get("value") == PW_VALUE
          and not any(PW_VALUE in a for a in hs[1]["argv"]) and PW_VALUE not in r.stdout + r.stderr, (r.stdout[-300:], [x.get("mode") for x in hs]))
    sflag("human-origin", SITE2)
    n5 = len(calls())
    r = client("fill", "web", "--vm", "cvm", "--field", "password", "--human")
    sunflag("human-origin")
    check("fill --human: an address bar naming another site is refused before the value goes", r.returncode == 2
          and not [x for x in calls()[n5:] if x.get("mode") == "fill"], r.stdout[-300:])
    sflag("human-deny-next")
    r = client("fill", "web", "--vm", "cvm", "--field", "password", "--human")
    check("fill --human: a focused non-password field is refused (the vault's own words)", r.returncode == 2
          and "isn't the kind of field this fills" in js(r.stdout).get("reason", ""), r.stdout[-300:])
    r = client("fill", "web", "--vm", "cvm", "--field", "password", "--human", "--selector", "#pw")
    check("fill --human takes no selector (the focused field is the target)", r.returncode == 64, r.stderr[-200:])
    n5 = len(calls())
    r = client("code", "--vm", "cvm", "--site", SITE, "--human", "--json", stdin=CODE + "\n")
    hs = [x for x in calls()[n5:] if x.get("human")]
    check("code --human under the rule: filled into the human-mode browser's focused field (otp), the value only on stdin",
          r.returncode == 0 and js(r.stdout).get("status") == "filled" and hs and json.loads(hs[-1]["stdin"]).get("field") == "otp"
          and json.loads(hs[-1]["stdin"]).get("value") == CODE, (r.stdout[-300:], hs))
    sflag("human-echo-next")
    r = client("code", "--vm", "cvm", "--site", SITE, "--human", "--json", stdin=CODE2 + "\n")
    check("human mode: a VM answer that spells the value is withheld", CODE2 not in r.stdout + r.stderr and r.returncode != 0, r.stdout[-300:])
    lock(u)

    # --- 8. a person at a terminal: give follows the approval path (typed at the unlock terminal here) -------------
    u = start_unlock()
    r = client("ask", "--vm", "cvm", "--site", SITE2, "--kind", "code", "--why", "terminal give", "--into", SITE2 + "/#otp", "--json")
    rid = js(r.stdout).get("request_id")
    g = ts.Pty([BIN, "give", rid], env)
    procs.append(g)
    g.expect("One-time code (hidden): ")
    g.send(CODE + "\n")
    u.expect(f"Request {rid}", 30)
    u.expect("Type ", 10)
    u.send(rid + "\n")
    rc = g.wait(60)
    gt = g.text()
    outputs.append(gt)
    g.close()
    check("give at a terminal reads the value hidden, then the unlock terminal asks (typed approvals here); typing the id fills it",
          rc == 0 and "filled" in gt and CODE not in gt, gt[-400:])
    check("...the unlock terminal showed the question and never the value", CODE not in u.text())
    r = client("ask", "--vm", "cvm", "--site", SITE2, "--kind", "code", "--why", "deny", "--json")
    rid = js(r.stdout).get("request_id")
    r = client("deny", rid)
    r2 = client("give", rid, stdin=CODE + "\n")
    check("deny ID refuses a waiting request; a give afterwards is refused", r.returncode == 0 and r2.returncode == 2, (r.stdout, r2.stdout[-200:]))
    lock(u)

    # --- 9. the dialog (INTERACTIVE_PROMPTS=on), with the osascript stand-in --------------------------------------------
    u = start_unlock(extra={"MCOMP_INTERACTIVE_PROMPTS": "on", "MCOMP_VAULT_TEST_OSASCRIPT": osa, "MCOMP_VAULT_TEST_LOCAL_AUTH": "allow"})
    marker = os.path.join(td, "pwned")
    nasty = f'Sign in "now" \\ ‮" & (do shell script "touch {marker}") & "'
    open(os.path.join(osadir, "answer"), "w").write(DIALOG_CODE + " ")
    n6 = len(calls())
    r = client("ask", "--vm", "cvm", "--site", SITE2, "--kind", "code", "--why", nasty, "--into", SITE2 + "/#otp", "--pattern", "alnum",
               "--length", "4-16", "--json")
    rid = js(r.stdout).get("request_id")
    check("ask with INTERACTIVE_PROMPTS=on says a dialog asks", js(r.stdout).get("dialog") is True, r.stdout[-200:])
    got = wait_for(lambda: [x for x in calls()[n6:] if x.get("mode") == "fill"], 15)
    oc = [json.loads(l) for l in open(os.path.join(osadir, "calls.jsonl"))] if os.path.exists(os.path.join(osadir, "calls.jsonl")) else []
    script_lines = [oc[0][i + 1] for i, x in enumerate(oc[0]) if x == "-e"] if oc else []
    words = [x for i, x in enumerate(oc[0]) if x != "-e" and (i == 0 or oc[0][i - 1] != "-e")] if oc else []
    check("the dialog gets the request's words as arguments, never in its script: the script is fixed, `why` arrives as one item",
          len(oc) == 1 and words[:2] == ["code", "127.0.0.1"] and words[3] == rid and "touch" in words[2] and "do shell script" in words[2]
          and not any(nasty[:10] in l or "touch" in l for l in script_lines), (words, script_lines[:3]))
    check("...nothing in `why` ran (no file made)", not os.path.exists(marker))
    check("the value from the dialog went straight to the vault and, approved (Touch ID stand-in), was filled; never in the dialog's arguments",
          len(got) == 1 and json.loads(got[0]["stdin"]).get("value") == DIALOG_CODE
          and not any(DIALOG_CODE in x for x in (oc[0] if oc else [])), got)
    os.remove(os.path.join(osadir, "answer"))
    r = client("ask", "--vm", "cvm", "--site", SITE2, "--kind", "code", "--why", "cancelled", "--json")
    rid = js(r.stdout).get("request_id")
    time.sleep(1.5)
    q = state(rid) or {}
    check("a cancelled dialog leaves the request pending (give it another way, or it expires)", q.get("state") == "pending", q)
    lock(u)
    check("the unlock terminals and every output show no value", not [v for v in VALUES if any(v in o for o in outputs)],
          [v for v in VALUES if any(v in o for o in outputs)])
    alltext = open(alog).read() if os.path.exists(alog) else ""
    check("the audit log holds no value", not [v for v in VALUES if v in alltext], [v for v in VALUES if v in alltext])
    evtext = json.dumps(events())
    check("the events hold no value", not [v for v in VALUES if v in evtext], [v for v in VALUES if v in evtext])


if __name__ == "__main__":
    sys.exit(main())
