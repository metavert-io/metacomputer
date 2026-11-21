#!/usr/bin/env python3
"""The live view on this Mac's own screen: `metacomputer view --open` (bin/metacomputer, do_view).

  liveview.py gui                       exit 0 when this command can use the Mac's own screen: it didn't come in
                                        over SSH, and the console is this user's screen session; else exit 1 and
                                        say why on stdout
  liveview.py clip [--clear-after 45]   the live-view password, one line on stdin -> the clipboard. A detached
                                        helper empties the clipboard after that many seconds, but only if nothing
                                        was copied since: it keeps the change count the copy's own clearContents
                                        returned (never the password), and compares and empties in one run.
  liveview.py remember --port PORT [--from-view]
                                        the live-view password, one line on stdin (typed at a terminal: not echoed) ->
                                        the login keychain item that Screen Sharing reads for vnc://127.0.0.1:PORT
                                        (created, or updated in place; then checked by its attributes). Prints
                                        "created" or "updated". Refused unless stdin is a terminal (a person typing
                                        it) or --from-view; refused while VIEW_REMEMBER is off
  liveview.py refresh --port PORT       the same, but only into metacomputer's item that's already there (never makes
                                        one): view --open's silent update. "updated", or exit 1. Refused while off
  liveview.py remembered --port PORT [--json]   exit 0 when that item is metacomputer's and there, 1 when it isn't,
                                        3 when the keychain couldn't say; --json prints its attributes (never the
                                        password)
  liveview.py forget --port PORT [--from-view]
                                        removes that item (only metacomputer's); prints "removed" or "none". Refused
                                        unless stdin is a terminal or --from-view

--from-view is internal: bin/metacomputer view passes it after its own checks (a terminal for a first save or a
removal, VIEW_REMEMBER). It says who's calling; it isn't proof of the user's consent. These checks guard against
mistakes (a script or an agent running the helper by itself), not against a program running as the user, which could
pass the flag, or reach the keychain without this helper, as with any keychain item it can reach (SECURITY.md).

VIEW_REMEMBER, as these apply it: the settings file's value (lib/labconfig.py: the file bin/metacomputer reads; empty
means ask, anything but ask, on or off means off), made stricter, never looser, by MCOMP_VIEW_REMEMBER from
bin/metacomputer (off > ask > on); a value in the environment that isn't one of those counts as off.

The clipboard is macOS's general pasteboard, through osascript's JavaScript bridge to AppKit (NSPasteboard), so
the copy and its change count, and later the comparison and the clearing, each happen inside one process. The
password is marked concealed and transient (nspasteboard.org's markers), which clipboard managers that follow
them don't keep. It comes in on stdin and leaves on osascript's stdin: never in an argument, the environment,
a file, a log or an event, and nothing here prints it. Stdlib only (and lib/labconfig.py).

Screen Sharing's remembered password (SECURITY.md, "The live view"). Screen Sharing (ScreenSharing.framework,
-[SSSessionView findCredentialInKeychainForAuthType:...]) looks for internet-password items in the login keychain
and keeps those whose protocol is 'vnc ', whose type is 'vncl' (a VNC password) or unset, and whose server and port
are its "standardized keychain identity" for the address: "HOST:PORT" run through NSURL, which for an IP address
gives the server "127.0.0.1:5977" with port 0 (a host name would give the name and the port); the newest match
wins. The item is written by osascript, not security(1): securityd puts an item into the partition of the program
that makes it, and security(1) has a partition of its own ("apple-tool:") that Apple's apps, Screen Sharing among
them ("apple:"), can't read without a keychain-password prompt (securityd clientid.cpp, acls.cpp). Its access list
names Screen Sharing and osascript, so neither asks. Keychain dialogs are switched off for the writing process:
a locked keychain fails instead of asking. The password reaches osascript on its stdin, as above; its arguments
are the address, the keychain's path, the item's label and a one-time tag for the check.
"""
import json
import os
import re
import resource
import secrets
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labconfig  # noqa: E402

MAX_PASSWORD = 256
CLEAR_DEFAULT_S = 45
# run(argv): "copy" (the text on stdin -> the pasteboard; prints the change count its clearContents returned, so
# it is this copy's own, or "raced" when the count moved before the text was in place: something else was copied
# in between) or "clear N" (empties the pasteboard only if its change count is still N; "cleared" or "kept").
PASTEBOARD_JS = r"""
ObjC.import('AppKit');
function run(argv) {
  var pb = $.NSPasteboard.generalPasteboard;
  if (argv[0] === 'copy') {
    var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var s = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding);
    if (s.isNil() || !ObjC.unwrap(s)) { return 'error'; }
    var n = pb.clearContents;
    var ok = pb.setStringForType(s, 'public.utf8-plain-text');
    pb.setStringForType($(''), 'org.nspasteboard.ConcealedType');
    pb.setStringForType($(''), 'org.nspasteboard.TransientType');
    if (!ok) { return 'error'; }
    return String(pb.changeCount) === String(n) ? String(n) : 'raced';
  }
  if (argv[0] === 'clear' && argv.length === 2) {
    if (String(pb.changeCount) !== argv[1]) { return 'kept'; }
    pb.clearContents;
    return 'cleared';
  }
  return 'error';
}
"""
COUNT_RE = re.compile(r"-?\d{1,20}")

SS_APP = "/System/Applications/Utilities/Screen Sharing.app"
LABEL_PREFIX = "metacomputer live view"
# run(argv) = [op, server, keychain path, label] + for "save" and "refresh": [tag, Screen Sharing's path]; the password
# is on stdin.
#   save    create the item (access: osascript and Screen Sharing), or update metacomputer's in place; then check
#           that the item found by its key carries this tag. "created" / "updated" / "foreign" / "error ..."
#   refresh only update metacomputer's item in place: "updated" / "none" (there isn't one; nothing made) / "foreign"
#   show    the attributes of the item found by its key, as JSON (null: none), never its password
#   forget  delete metacomputer's item: "removed" / "none" / "foreign" / "error ..."
# The item's key: server, protocol 'vnc ', port 0, empty account, authentication type 'dflt' (Screen Sharing's own
# items have none, so they're never this one). Attribute lists are built in one buffer whose pointers come from its
# own address (memmove(p, p, 0) returns p).
REMEMBER_JS = r"""
ObjC.import('Foundation');
ObjC.import('Security');
function run(argv) {
  var op = argv[0], server = argv[1], kcPath = argv[2], label = argv[3];
  ObjC.bindFunction('SecKeychainSetUserInteractionAllowed', ['int', ['bool']]);
  ObjC.bindFunction('SecKeychainOpen', ['int', ['char *', 'id *']]);
  ObjC.bindFunction('SecTrustedApplicationCreateFromPath', ['int', ['char *', 'id *']]);
  ObjC.bindFunction('SecAccessCreate', ['int', ['id', 'id', 'id *']]);
  ObjC.bindFunction('SecKeychainItemCreateFromContent',
                    ['int', ['unsigned int', 'void *', 'unsigned int', 'void *', 'id', 'id', 'void *']]);
  ObjC.bindFunction('SecKeychainFindInternetPassword',
                    ['int', ['id', 'unsigned int', 'void *', 'unsigned int', 'void *', 'unsigned int', 'void *',
                             'unsigned int', 'void *', 'unsigned short', 'unsigned int', 'unsigned int', 'void *',
                             'void *', 'id *']]);
  ObjC.bindFunction('SecKeychainItemModifyAttributesAndData', ['int', ['id', 'void *', 'unsigned int', 'void *']]);
  ObjC.bindFunction('SecKeychainItemDelete', ['int', ['id']]);
  ObjC.bindFunction('SecItemCopyMatching', ['int', ['id', 'id *']]);
  ObjC.bindFunction('memmove', ['unsigned long long', ['void *', 'void *', 'unsigned long long']]);
  $.SecKeychainSetUserInteractionAllowed(false);
  function fcc(s) { return ((s.charCodeAt(0) << 24) | (s.charCodeAt(1) << 16) | (s.charCodeAt(2) << 8) | s.charCodeAt(3)) >>> 0; }
  function ascii(s) { var a = []; for (var i = 0; i < s.length; i++) { a.push(s.charCodeAt(i) & 255); } return a; }
  function u32(v) { return [v & 255, (v >>> 8) & 255, (v >>> 16) & 255, (v >>> 24) & 255]; }
  function attrList(attrs) {
    var n = attrs.length, size = 16 + 16 * n, offs = [];
    attrs.forEach(function (a) { offs.push(size); size += (a[1].length + 7) & ~7; });
    var buf = $.NSMutableData.dataWithLength(size);
    var base = BigInt($.memmove(buf.mutableBytes, buf.mutableBytes, 0));
    var b = new Array(size).fill(0);
    function put(o, bytes) { for (var i = 0; i < bytes.length; i++) { b[o + i] = bytes[i]; } }
    function ptr(o, v) { for (var k = 0; k < 8; k++) { b[o + k] = Number((v >> BigInt(8 * k)) & 255n); } }
    put(0, u32(n)); ptr(8, base + 16n);
    attrs.forEach(function (a, i) {
      put(16 + 16 * i, u32(fcc(a[0]))); put(20 + 16 * i, u32(a[1].length));
      if (a[1].length) { ptr(24 + 16 * i, base + BigInt(offs[i])); put(offs[i], a[1]); }
    });
    var src = $(String.fromCharCode.apply(null, b)).dataUsingEncoding($.NSISOLatin1StringEncoding);
    if (src.isNil() || Number(src.length) !== size) { throw new Error('attribute list'); }
    $.memmove(buf.mutableBytes, src.bytes, size);
    return buf;
  }
  var kcRef = Ref();
  var st = $.SecKeychainOpen(kcPath, kcRef);
  if (st !== 0) { return 'error open ' + st; }
  var kc = kcRef[0];
  var noAccount = $.NSMutableData.dataWithLength(8);
  var srv = $(server).dataUsingEncoding($.NSUTF8StringEncoding);
  function find() {
    var item = Ref();
    var st = $.SecKeychainFindInternetPassword(kc, srv.length, srv.bytes, 0, null, 0, noAccount.mutableBytes, 0, null,
                                               0, fcc('vnc '), fcc('dflt'), null, null, item);
    if (st === -25300) { return null; }
    if (st !== 0) { throw new Error('find ' + st); }
    return item[0];
  }
  function attrs(item) {
    var q = $.NSMutableDictionary.dictionary, res = Ref(), list = $.NSMutableArray.array;
    list.addObject(item);
    q.setObjectForKey('inet', 'class'); q.setObjectForKey(list, 'u_ItemList');
    q.setObjectForKey(true, 'r_Attributes'); q.setObjectForKey('m_LimitAll', 'm_Limit');
    var st = $.SecItemCopyMatching(q, res);
    if (st !== 0 || Number(res[0].count) !== 1) { throw new Error('attributes ' + st); }
    var d = res[0].objectAtIndex(0);
    function s(k) { var v = d.objectForKey(k); return v.isNil() ? null : ObjC.unwrap(v.description); }
    return {server: s('srvr'), port: s('port'), account: s('acct'), label: s('labl'), comment: s('icmt'),
            type: s('type')};
  }
  try {
    var item = find(), a = item === null ? null : attrs(item);
    if (op === 'show') { return JSON.stringify(a); }
    if (a !== null && a.label !== label) { return 'foreign'; }
    if (op === 'forget') {
      if (item === null) { return 'none'; }
      st = $.SecKeychainItemDelete(item);
      return st === 0 ? 'removed' : 'error delete ' + st;
    }
    if ((op !== 'save' && op !== 'refresh') || argv.length !== 6) { return 'error usage'; }
    if (op === 'refresh' && item === null) { return 'none'; }
    var tag = argv[4], app = argv[5], how;
    var pw = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    if (pw.isNil() || pw.length < 1 || pw.length > 256) { return 'error password'; }
    if (item === null) {
      var me = Ref(), ss = Ref(), acc = Ref(), trusted = $.NSMutableArray.array;
      if ($.SecTrustedApplicationCreateFromPath('/usr/bin/osascript', me) !== 0) { return 'error trusted osascript'; }
      if ($.SecTrustedApplicationCreateFromPath(app, ss) !== 0) { return 'error trusted app'; }
      trusted.addObject(me[0]); trusted.addObject(ss[0]);
      if ($.SecAccessCreate($(label), trusted, acc) !== 0) { return 'error access'; }
      var made = attrList([['srvr', ascii(server)], ['ptcl', u32(fcc('vnc '))], ['port', u32(0)], ['acct', []],
                           ['atyp', u32(fcc('dflt'))], ['type', u32(fcc('vncl'))], ['labl', ascii(label)],
                           ['icmt', ascii(tag)]]);
      st = $.SecKeychainItemCreateFromContent(fcc('inet'), made.mutableBytes, pw.length, pw.bytes, kc, acc[0], null);
      how = 'created';
    } else {
      var changed = attrList([['labl', ascii(label)], ['icmt', ascii(tag)], ['type', u32(fcc('vncl'))]]);
      st = $.SecKeychainItemModifyAttributesAndData(item, changed.mutableBytes, pw.length, pw.bytes);
      how = 'updated';
    }
    if (st !== 0) { return 'error ' + how + ' ' + st; }
    item = find();
    a = item === null ? null : attrs(item);
    if (a === null || a.comment !== tag || a.label !== label || a.server !== server || a.port !== '0'
        || a.type !== String(fcc('vncl'))) { return 'error check'; }
    return how;
  } catch (e) {
    return 'error ' + e.message;
  }
}
"""
KEYCHAIN_ERRORS = {"-25308": "the login keychain is locked (or would have asked: keychain dialogs are off here)",
                   "-25294": "there's no login keychain", "-25293": "the keychain refused it (not permitted)",
                   "-25299": "an item for this address is already there"}


def pasteboard(args, data=None):
    """osascript with PASTEBOARD_JS: its one line of output, or None."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", PASTEBOARD_JS] + args, input=data,
                           capture_output=True, timeout=15, stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def gui_reason():
    """None when this process can put a window on the Mac's own screen; else why not."""
    for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        if os.environ.get(k):
            return "this terminal came in over SSH, so it isn't on the Mac's own screen"
    try:
        if os.stat("/dev/console").st_uid != os.getuid():
            return "the Mac's screen isn't yours right now (another user, or the login window)"
    except OSError:
        return "this Mac has no screen session"
    try:
        r = subprocess.run(["launchctl", "managername"], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        manager = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        manager = ""
    if manager != "Aqua":
        return "this process isn't part of the Mac's screen session"
    return None


def read_hidden():
    """One line typed at the terminal that is stdin (never /dev/tty), with echo off on it while it's read and a prompt
    on stderr; echo comes back afterwards, even after Ctrl-C. b"" when echo can't be turned off (nothing is read)."""
    import termios
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSAFLUSH, quiet)
    except (termios.error, OSError):
        return b""
    try:
        sys.stderr.write("Live-view password (it isn't shown): ")
        sys.stderr.flush()
        return sys.stdin.buffer.readline(MAX_PASSWORD + 2)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        except (termios.error, OSError):
            pass
        sys.stderr.write("\n")
        sys.stderr.flush()


def read_password():
    """One line from stdin: 1-256 bytes, no control characters (VNC itself uses the first 8). Piped in, as
    bin/metacomputer does; typed at a terminal, not echoed (read_hidden)."""
    data = read_hidden() if sys.stdin.isatty() else sys.stdin.buffer.readline(MAX_PASSWORD + 2)
    pw = data.rstrip(b"\r\n")
    if not pw or len(pw) > MAX_PASSWORD or any(c < 32 or c == 127 for c in pw):
        return None
    return pw


def _max_fd():
    try:
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (OSError, ValueError):
        soft = 1024
    return min(soft if soft > 0 else 1024, 65536)


def clear_later(count, after):
    """A detached helper (its own session, no inherited descriptors: not the caller's output pipe, nor the VM
    locks on 7 and 8) that empties the clipboard after `after` seconds if its change count is still `count`."""
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, _max_fd())
        time.sleep(after)
        pasteboard(["clear", count])
    except BaseException:
        pass
    finally:
        os._exit(0)


def clip(clear_after):
    pw = read_password()
    if pw is None:
        print("couldn't read the live-view password from its store", file=sys.stderr)
        return 1
    count = pasteboard(["copy"], pw)
    del pw
    if count == "raced":
        # Something else was copied while the password went on: which is on the clipboard now is unclear, so
        # nothing is cleared later (it could be someone else's).
        print("something else was copied at the same moment, so the clipboard won't be cleared by itself; copy "
              "something else when you're done", file=sys.stderr)
        return 0
    if count is None or not COUNT_RE.fullmatch(count):
        print("couldn't put the live-view password on the clipboard (osascript failed)", file=sys.stderr)
        return 1
    if clear_after > 0:
        clear_later(count, clear_after)
    return 0


# --- Screen Sharing's remembered password ----------------------------------------------------------------
def ss_server(port):
    """Screen Sharing's server attribute for vnc://127.0.0.1:PORT (its port attribute is then 0)."""
    return f"127.0.0.1:{port}"


def ss_label(port):
    return f"{LABEL_PREFIX} ({ss_server(port)})"


def login_keychain():
    """The login keychain's path, from `security login-keychain` (one quoted path), or None."""
    try:
        r = subprocess.run(["security", "login-keychain"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.fullmatch(r'\s*"(/[^"\\\n]+)"\s*', r.stdout.splitlines()[0] if r.stdout else "")
    if r.returncode != 0 or not m or not os.path.isfile(m.group(1)):
        return None
    return m.group(1)


def keychain_js(args, data=None):
    """osascript with REMEMBER_JS: its one line of output, or "error ..."."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", REMEMBER_JS] + args,
                           input=data if data is not None else b"", capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"error osascript {type(e).__name__}"
    out = r.stdout.decode("utf-8", "replace").strip()
    return out if r.returncode == 0 and out else "error osascript exited " + str(r.returncode)


def explain(result):
    """Words for an "error ..." from REMEMBER_JS (codes are the Security framework's)."""
    code = next((w for w in result.split() if re.fullmatch(r"-\d{3,6}", w)), None)
    return KEYCHAIN_ERRORS.get(code, result[len("error "):][:120] if result.startswith("error ") else result[:120])


def port_arg(argv):
    if len(argv) >= 2 and argv[0] == "--port" and argv[1].isdigit() and 1 <= int(argv[1]) <= 65535:
        return int(argv[1]), argv[2:]
    return None, argv


ORDER = {"on": 0, "ask": 1, "off": 2}


def remember_setting(env=None):
    """VIEW_REMEMBER as the settings file says (empty or unset: ask; anything but ask, on or off: off, as
    bin/metacomputer reads it), made stricter, never looser, by MCOMP_VIEW_REMEMBER; an unknown value in the
    environment counts as off too."""
    env = os.environ if env is None else env
    try:
        conf = labconfig.load(env).get("VIEW_REMEMBER", "ask")
    except Exception:
        conf = "off"
    conf = "ask" if conf == "" else conf
    conf = conf if conf in ORDER else "off"
    if "MCOMP_VIEW_REMEMBER" not in env:
        return conf
    given = env["MCOMP_VIEW_REMEMBER"]
    given = given if given in ORDER else "off"
    return max(conf, given, key=ORDER.get)


def consent_refusal(what, from_view):
    """None when stdin is the user's own terminal (a terminal with no agent session above it: Codex can give its
    commands a pseudo-terminal), or the caller is bin/metacomputer view (--from-view), which checked first. A guard
    against mistakes, not a barrier (the docstring above)."""
    import owners
    if from_view or owners.at_user_terminal((0,)):
        return None
    return (f"{what} changes your login keychain, so it needs your yes: run `metacomputer view --remember` (or "
            "--forget) in your own terminal")


def remember(port, op="save", from_view=False):
    if op == "save":
        why = consent_refusal("saving the live-view password for Screen Sharing", from_view)
        if why:
            print(why, file=sys.stderr)
            return 1
    if remember_setting() == "off":
        print("VIEW_REMEMBER is off, so nothing is saved for Screen Sharing (set it to ask to change that)",
              file=sys.stderr)
        return 1
    pw = read_password()
    if pw is None:
        print("couldn't read the live-view password from its store", file=sys.stderr)
        return 1
    kc = login_keychain()
    if kc is None:
        print("couldn't find the login keychain (security login-keychain)", file=sys.stderr)
        return 1
    tag = "mcomp-" + secrets.token_hex(10)
    result = keychain_js([op, ss_server(port), kc, ss_label(port), tag, SS_APP], pw)
    del pw
    if result in ("created", "updated"):
        print(result)
        return 0
    if result == "none":
        print(f"there's no saved live-view password of metacomputer's for {ss_server(port)} to bring up to date",
              file=sys.stderr)
        return 1
    if result == "foreign":
        print(f"the login keychain has an item for {ss_server(port)} that metacomputer didn't make, so it was left "
              "alone (Keychain Access shows it)", file=sys.stderr)
    else:
        print(f"couldn't save it in the login keychain: {explain(result)}", file=sys.stderr)
    return 1


def remembered(port, as_json):
    kc = login_keychain()
    result = keychain_js(["show", ss_server(port), kc, ss_label(port)]) if kc else "error no login keychain"
    try:
        attrs = json.loads(result)
    except ValueError:
        if as_json:
            print(json.dumps({"error": explain(result)}))
        return 3
    if as_json:
        print(json.dumps(attrs))
    return 0 if isinstance(attrs, dict) and attrs.get("label") == ss_label(port) else 1


def forget(port, from_view=False):
    why = consent_refusal("removing Screen Sharing's saved live-view password", from_view)
    if why:
        print(why, file=sys.stderr)
        return 1
    kc = login_keychain()
    if kc is None:
        print("couldn't find the login keychain (security login-keychain)", file=sys.stderr)
        return 1
    result = keychain_js(["forget", ss_server(port), kc, ss_label(port)])
    if result in ("removed", "none"):
        print(result)
        return 0
    if result == "foreign":
        print(f"the item for {ss_server(port)} in the login keychain isn't metacomputer's; left alone",
              file=sys.stderr)
    else:
        print(f"couldn't remove it: {explain(result)}", file=sys.stderr)
    return 1


def main(argv):
    if argv[:1] in (["remember"], ["refresh"], ["remembered"], ["forget"]):
        port, rest = port_arg(argv[1:])
        extra = {"remembered": ["--json"], "remember": ["--from-view"], "forget": ["--from-view"]}
        if port is None or rest not in ([], extra.get(argv[0], [])):
            print(f"usage: liveview.py {argv[0]} --port PORT" + "".join(f" [{x}]" for x in extra.get(argv[0], [])),
                  file=sys.stderr)
            return 2
        if argv[0] in ("remember", "refresh"):
            return remember(port, "save" if argv[0] == "remember" else "refresh", bool(rest))
        return remembered(port, bool(rest)) if argv[0] == "remembered" else forget(port, bool(rest))
    if argv[:1] == ["gui"]:
        why = gui_reason()
        if why:
            print(why)
            return 1
        return 0
    if argv[:1] == ["clip"]:
        after = CLEAR_DEFAULT_S
        if len(argv) == 3 and argv[1] == "--clear-after":
            try:
                after = max(0, min(int(argv[2]), 3600))
            except ValueError:
                print("--clear-after takes seconds", file=sys.stderr)
                return 2
        elif len(argv) != 1:
            print("usage: liveview.py clip [--clear-after SECONDS]", file=sys.stderr)
            return 2
        return clip(after)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
