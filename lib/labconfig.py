"""Read simple KEY=VALUE settings from config/defaults.env, then the one settings file scripts/lib.sh reads:
$MCOMP_CONFIG, else config/vms/NAME.env for MCOMP_VM=NAME, else the default computer that config/default-vm names
(unless MCOMP_NO_DEFAULT_VM is set), else this machine's config/local.env. Only plain values are understood (KEY=value,
optionally after export; quotes stripped, no expansion), which is all the Python side needs (e.g. GUEST_USER); what
the handoff gates tell computers by (computers()) is read more strictly, below. scripts/lib.sh refuses the unclear
cases before anything runs, and bin/metacomputer passes the VM it chose on to what it starts (MCOMP_CONFIG, MCOMP_VM
or MCOMP_NO_DEFAULT_VM), so this only has to pick the same file.

The default computer is read by lib/defaultvm.py, as scripts/lib.sh reads it, and resolved by scripts/lib.sh's own
mc_load_settings (every check --vm NAME gets: its name, ports, folders and every other VM's), in a bash that reads
no startup file and imports no function from the environment. That bash hands back the settings it loaded, as
values, in one strict record on a pipe of its own; they're used as they are, and the file is never read again here,
so what was checked is what is used. The pipe is a named one in a private folder, opened for writing only by the
step that writes the record, after the settings are loaded: nothing the resolution started (a helper left running in
the background) holds it, so the answer comes when that bash exits. The bash runs in a session of its own, and if it
takes too long or says too much, that whole process group is killed, not only the bash. Anything else (a refusal, a
record that isn't exactly that, a bash that fails) raises Refused: it never falls back to another VM, nor to the
defaults."""
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defaultvm  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMS_DIR = os.path.join(LAB, "config", "vms")
MAIN_CONFIG = os.path.join(LAB, "config", "local.env")
DEFAULT_VM_FILE = os.path.join(LAB, "config", "default-vm")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s#]*)")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
# What makes a VM itself: a file in config/vms/ never takes these from the defaults (scripts/lib.sh).
IDENTITY = ("VM_NAME", "BASELINE_NAME", "SSH_DIR", "SSH_PORT", "VNC_PORT", "RUN_DIR", "LOG_DIR")
Refused = defaultvm.Refused
# What a bash would run, or how it would start, if the environment said so: never passed to the resolver.
_SHELL_ENV = ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH", "GLOBIGNORE", "PS4", "PROMPT_COMMAND",
              "BASH_XTRACEFD", "IFS")
_SELECTION = ("MCOMP_VM", "MCOMP_CONFIG", "MCOMP_NO_DEFAULT_VM")
_MAGIC, _MAX_RECORD = b"MCOMP-SETTINGS/1", 1 << 20
_DEADLINE = 120         # seconds the resolution may take
# scripts/lib.sh's resolution of the default computer NAME ($2), as --vm NAME resolves it. Its result goes only to the
# named pipe $3, which only the step that writes it opens (with "$3", the script's own argument: nothing the settings
# file sets can move it), NUL-separated: MCOMP-SETTINGS/1, then FILE PATH and KEY VALUE pairs (every name assigned in
# defaults.env or the file, and what makes a VM itself, with the value the loaded settings give it), or REFUSED
# REASON; then END. Nothing it or what it runs prints anywhere else is read, and nothing it runs is given the pipe.
_RESOLVE = r"""
MC_LIB_DEFS_ONLY=1 . "$1/scripts/lib.sh" >/dev/null 2>&1 || exit 3
if ! mc_load_settings "$2" "" >/dev/null 2>&1; then
  printf '%s\0' MCOMP-SETTINGS/1 REFUSED "$MC_REFUSAL" END >"$3"
  exit 1
fi
names="$(sed -n 's/^[[:space:]]*\([A-Z_][A-Z0-9_]*\)=.*/\1/p' "$1/config/defaults.env" "$LOCAL_CONFIG" 2>/dev/null)"
names="$(printf '%s\n' $names VM_NAME BASELINE_NAME SSH_DIR SSH_PORT VNC_PORT RUN_DIR LOG_DIR VAULT_DIR EVENTS_SPOOL | sort -u)"
{ printf '%s\0' MCOMP-SETTINGS/1 FILE "$LOCAL_CONFIG"
  for n in $names; do
    if [ -n "${!n+x}" ]; then printf '%s\0' "$n" "${!n}"; fi
  done
  printf '%s\0' END; } >"$3"
"""


def read_default_vm(path=None):
    """config/default-vm's name (lib/defaultvm.py, the reader scripts/lib.sh uses), or None; Refused otherwise."""
    return defaultvm.read(DEFAULT_VM_FILE if path is None else path)


def _scrubbed(env):
    """The resolver's environment: no VM already chosen, nothing that changes how a bash starts, no function."""
    out = {}
    for k, v in env.items():
        if k in _SELECTION or k in _SHELL_ENV or k.startswith("BASH_FUNC_") or "%" in k or v.startswith("()"):
            continue
        out[k] = v
    out.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return out


def decode_record(data, returncode):
    """The resolver's record -> (settings file, {KEY: VALUE}); Refused for a refusal, and for anything that isn't
    exactly one well-formed record from a bash that exited 0."""
    parts = data.split(b"\0")
    if not data.endswith(b"\0") or len(parts) < 3 or parts[0] != _MAGIC or parts[-2] != b"END":
        raise Refused("the check of its settings didn't answer as it should")
    body = parts[1:-2]
    if body[:1] == [b"REFUSED"]:
        if len(body) != 2 or returncode != 1:
            raise Refused("the check of its settings didn't answer as it should")
        raise Refused(body[1].decode("utf-8", "replace") or "its settings were refused")
    if returncode != 0 or len(body) < 2 or body[0] != b"FILE" or len(body) % 2:
        raise Refused("the check of its settings didn't answer as it should")
    try:
        path = body[1].decode("utf-8")
        vals = {}
        for k, v in zip(body[2::2], body[3::2]):
            key = k.decode("ascii")
            if not _KEY.fullmatch(key) or key in vals:
                raise ValueError(key)
            vals[key] = v.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        raise Refused("the check of its settings didn't answer as it should") from None
    if not path.startswith("/") or not _NAME.fullmatch(vals.get("VM_NAME", "")):
        raise Refused("the check of its settings didn't answer as it should")
    return path, vals


def _read(fd, n=65536):
    """Up to n bytes from the record's pipe; b"" when there's nothing there (yet, or any more)."""
    try:
        return os.read(fd, n)
    except BlockingIOError:
        return b""


class _ExitWatch:
    """Whether the resolver p has exited, told without reaping it. Until it's reaped, its pid can't be given to another
    process, so no other process group can have its id: killing the group it leads reaches only what it started.
    os.waitid with WNOWAIT where Python has it; else (macOS's Python before 3.13) a kqueue watch for its exit. lost:
    whether it's still unreaped can't be told (the system says it has no such child: something else reaped it), so
    its group's id is no longer certainly its own: nothing may be sent to it, and the check refuses."""
    # This relies on nothing else in this process reaping children it didn't start: no waitpid(-1) or os.wait(),
    # wait3 or wait4, no SIGCHLD handler and no SIG_IGN for it, no Safe or Fast child watcher (tests/test_multi_vm.py,
    # reap_inventory, fails on any of them in lib/, bin/ or scripts/). Between a check here and a signal, a reaper
    # like that could still free the id; that race is accepted only because nothing here does it.

    def __init__(self, p):
        """Nothing that can fail here: the caller holds the watch before start() does anything that can. Until the
        kqueue watch is in place it counts as lost, so a start() that fails or is interrupted never allows a signal
        unless it's known that the resolver wasn't lost (a registration refused for another reason)."""
        self.by_waitid = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
        self.p, self.kq, self.done, self.lost = p, None, False, not self.by_waitid
        self.reaped, self.status = False, None      # reaped here already (start()), with its own exit status

    def start(self):
        if self.by_waitid:
            return
        if not hasattr(select, "kqueue"):
            self.lost = False           # nothing was seen: it's still its own child
            raise OSError("this Python can't tell it has exited without reaping it")
        self.kq = select.kqueue()
        try:
            self.kq.control([select.kevent(self.p.pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)],
                            0, 0)
        except ProcessLookupError:
            # Gone before the watch began. macOS says so for its own unreaped zombie too, so ask: reaping it is the
            # only way to tell. Ours (its pid back): it's done and reaped, with its own exit status, and from here on
            # nothing is sent to its group (its id is no longer certainly its own), so helpers it left are left even
            # if the check then refuses: the accepted cost of this one ordering. No such child: something else reaped
            # it; still running can't be: either way it stays lost.
            self.done = True
            self.close()
            try:
                pid, status = os.waitpid(self.p.pid, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == self.p.pid:
                self.status = os.waitstatus_to_exitcode(status)
                self.p.returncode = self.status
                self.reaped = True
                self.lost = False
            return
        except OSError:                 # refused for another reason: it's still its own (unreaped) child
            self.lost = False
            self.close()
            raise
        except BaseException:           # interrupted: whether it's lost can't be told; it stays lost
            self.close()
            raise
        self.lost = False

    def exited(self):
        if not self.done:
            try:
                if self.by_waitid:
                    self.done = os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                else:
                    self.done = bool(self.kq.control(None, 1, 0))
            except ChildProcessError:   # no such child any more: something else reaped it
                self.done = self.lost = True
        return self.done

    def ours(self):
        """Whether it's still certainly unreaped, so its group's id its own: checked again right before a signal, where
        that can be told without reaping it (waitid); the kqueue watch can only say it was never lost."""
        if self.reaped:
            return False
        if self.by_waitid and not self.lost:
            try:
                os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except OSError:             # no such child any more (or it can't be told): nothing is sent
                self.lost = True
        return not self.lost

    def close(self):
        kq, self.kq = self.kq, None
        if kq is not None:
            kq.close()


def _reap(p):
    """Reap the resolver: its own exit status, or None when it's lost (something else reaped it): never Popen's
    stand-in 0. Popen is told it's done, so it never waits for it again."""
    try:
        status = os.waitpid(p.pid, 0)[1]
        rc = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        rc = None
    p.returncode = -1 if rc is None else rc     # for Popen only (-1 isn't a status anyone reads)
    return rc


def _end(p, kill):
    """The resolver's whole process group (it leads it: its session's) killed when kill (only while it's certainly
    unreaped and its own), then the resolver reaped; its exit status, or None when it's lost."""
    if kill:
        for send in (lambda: os.killpg(p.pid, signal.SIGKILL), lambda: os.kill(p.pid, signal.SIGKILL)):
            try:
                send()
            except OSError:
                pass
    return _reap(p)


_LOST = "the check of its settings was lost (something else reaped it, so its exit status can't be known)"


def _collect(p, r, why):
    """(the record the resolver p wrote to the pipe r, its exit status). p isn't reaped until its record is whole and
    its size checked: on every refusal (it takes too long, it says too much, while it runs or in what's left once it
    has exited) and on an interruption before the final reap, its whole process group is killed first, then it's
    reaped. Once its ownership is lost, nothing is sent and the check refuses; so does an exit status that's lost."""
    data, end, watch, reaping = b"", time.monotonic() + _DEADLINE, None, False
    try:
        watch = _ExitWatch(p)
        watch.start()
        while not watch.exited():
            left = end - time.monotonic()
            if left <= 0 or len(data) > _MAX_RECORD:
                raise Refused(why + "the check of its settings took too long or said too much")
            if select.select([r], [], [], min(left, 0.05))[0]:
                chunk = _read(r)
                if chunk:
                    data += chunk
                    continue
                time.sleep(min(left, 0.01))     # its end (the record's step closed it): the bash is exiting
        if watch.lost:
            raise Refused(why + _LOST)
        # It has exited, unreaped: what's still in the pipe is the end of its record (at most a pipe's buffer).
        while len(data) <= _MAX_RECORD:
            chunk = _read(r)
            if not chunk:
                break
            data += chunk
        if len(data) > _MAX_RECORD:
            raise Refused(why + "the check of its settings said too much")
        watch.close()
        reaping = True                          # from here on, nothing is sent: it may already be reaped
        rc = watch.status if watch.reaped else _reap(p)     # only now; what outlives it is left as it is
    except BaseException as e:
        try:
            _end(p, kill=not reaping and watch is not None and watch.ours())     # never on a watch never built
        finally:
            if watch is not None:
                watch.close()
        if isinstance(e, OSError):
            raise Refused(why + f"its settings couldn't be checked ({e})") from None
        raise
    if rc is None:
        raise Refused(why + _LOST)
    return data, rc


def resolve_default(name, env=None):
    """(settings file, the settings as scripts/lib.sh loaded them) for the default computer NAME, exactly as --vm
    NAME resolves it; Refused otherwise."""
    why = f"config/default-vm makes {name} your default computer, but: "
    r = folder = fifo = None
    try:
        try:
            folder = tempfile.mkdtemp(prefix="mcomp-settings-")      # 0700, this account's
            fifo = os.path.join(folder, "record")
            os.mkfifo(fifo, 0o600)
            r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)         # not inherited (PEP 446); no writer yet
            p = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-p", "-c", _RESOLVE, "_", LAB, name, fifo],
                                 env=_scrubbed(os.environ if env is None else env), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise Refused(why + f"its settings couldn't be checked ({e})") from None
        data, rc = _collect(p, r, why)
    finally:
        if r is not None:
            os.close(r)
        for path, remove in ((fifo, os.unlink), (folder, os.rmdir)):
            try:
                if path:
                    remove(path)
            except OSError:
                pass
    try:
        return decode_record(data, rc)
    except Refused as e:
        raise Refused(why + str(e)) from None


def default_settings_path(name, env=None):
    """The settings file the default computer NAME resolves to (resolve_default); Refused otherwise."""
    return resolve_default(name, env)[0]


def _selection(env=None):
    """(the settings file, the settings scripts/lib.sh resolved for it or None to parse it here)."""
    env = os.environ if env is None else env
    if env.get("MCOMP_CONFIG"):
        return env["MCOMP_CONFIG"], None
    name = env.get("MCOMP_VM") or ""
    if name:
        if _NAME.fullmatch(name):
            p = os.path.join(VMS_DIR, name + ".env")
            if os.path.isfile(p):
                return p, None
        return MAIN_CONFIG, None
    if not env.get("MCOMP_NO_DEFAULT_VM"):
        pointer = read_default_vm()
        if pointer:
            return resolve_default(pointer, env)
    return MAIN_CONFIG, None


def settings_path(env=None):
    return _selection(env)[0]


def vm_file_name(path):
    """NAME when path is config/vms/NAME.env (links in its folder resolved), else None."""
    try:
        d = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if d == os.path.realpath(VMS_DIR) and path.endswith(".env"):
            return os.path.basename(path)[:-4]
    except (OSError, ValueError):
        pass
    return None


def _parse(path):
    vals = {}
    try:
        for line in open(path):
            m = _LINE.match(line)
            if m:
                vals[m.group(1)] = m.group(2).strip("\"'")
    except OSError:
        pass
    return vals


def load(env=None):
    path, resolved = _selection(env)
    if resolved is not None:
        return dict(resolved)       # the default computer's settings as scripts/lib.sh checked them: never re-read
    vals = _parse(os.path.join(LAB, "config", "defaults.env"))
    vm = vm_file_name(path)
    if vm:
        for k in IDENTITY:
            vals.pop(k, None)
    vals.update(_parse(path))
    if vm:
        vals["VM_NAME"] = vals.get("VM_NAME") or vm
        vals["BASELINE_NAME"] = vals.get("BASELINE_NAME") or vals["VM_NAME"] + "-baseline"
    return vals


# The main computer's name when its settings don't give one (config/defaults.env says the same): every Python helper
# that needs it falls back on this one (lib/storage.py vm_name, computers() below).
DEFAULT_VM_NAME = "metacomputer"

# --- the settings a handoff gate tells computers by (computers()), read strictly ----------------------------------
# scripts/lib.sh sources a settings file as bash. This reads it without running anything, and only when the whole file
# is plain assignments: every line is blank, a comment, or exactly one
#   [export ]NAME=VALUE, then optional spaces or tabs and an optional # comment,
# whose VALUE is one unquoted word (letters, digits, / . _ - : @ % + , =, a leading ~ or ~/, $NAME or ${NAME}), one
# "double-quoted" string (no backslash or backquote, $ only as $NAME or ${NAME}) or one 'single-quoted' string (or
# several joined by \', as `vm create` writes a quote in a value). Any other line, anywhere in the file, raises Unclear:
# a command, a $var command, control flow, a function, a here-document, a line continued or chained (\, &&, ||, |, ;,
# &), a value with anything else in it (;&|<>() or a backquote, $(, a space, a ~ past the start, a backslash), a
# control character other than a tab anywhere (a NUL, a carriage return). So does
# an assignment to a variable this reader puts into values (EXPANDED: HOME, LAB_ROOT), since bash would use the new
# value from there on. A setting it can't read is never taken as missing (a default: another computer's folder). A
# value's $HOME, $LAB_ROOT (and ~ where bash expands it) are read; a folder that uses any other variable, or isn't
# absolute, is None (it can't be told), a name or place that does raises.
EXPANDED = ("HOME", "LAB_ROOT")


class Unclear(ValueError):
    """A settings file that isn't plain assignments only (computers()): which computer it is can't be told."""


# A name is all of its letters, as bash takes it (never split into a shorter name and text: the lookahead also keeps a
# line that doesn't match from taking exponential time to say so).
_REF = r"\$(?:[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_])|\{[A-Za-z_][A-Za-z0-9_]*\})"
_ASSIGN = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="
                     r"(\"(?:[^\"\\`$\n]|" + _REF + r")*\"|'[^'\n]*'(?:\\''[^'\n]*')*"
                     r"|(?:~(?=/|[ \t]|$))?(?:[A-Za-z0-9_./:@%+,=-]|" + _REF + r")*)(?:[ \t]+(?:#.*)?)?")
_BLANK = re.compile(r"[ \t]*(?:#.*)?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")     # any but a tab (a NUL: bash 3.2 stops reading there)


def _label(path):
    home = os.environ.get("HOME") or ""
    return "~" + path[len(home):] if home and path.startswith(home + "/") else path


def _parse_strict(path, missing_ok=False):
    """{NAME: its value as written} from the settings file at PATH, read as the notes above say: a file of plain
    assignments only. A missing file is {} when MISSING_OK; one that is there but can't be read raises OSError; any
    line that isn't blank, a comment or one plain assignment, an assignment to HOME or LAB_ROOT, a control character
    other than a tab, and text that isn't UTF-8 raise Unclear (a ValueError)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Unclear(f"{_label(path)} isn't UTF-8 text, so its settings can't be told") from None
    vals = {}
    for n, line in enumerate(text.split("\n"), 1):     # a carriage return is part of the line, as bash reads it
        c = _CONTROL.search(line)
        if c:
            raise Unclear(f"{_label(path)} line {n} has a control character ({ord(c.group(0)):#04x}), which bash may stop "
                          "reading at or read otherwise, so its settings can't be told")
        if _BLANK.fullmatch(line):
            continue
        m = _ASSIGN.fullmatch(line)
        if not m:
            raise Unclear(f"{_label(path)} line {n} isn't a plain assignment (only NAME=value, NAME=\"value\" or "
                          "NAME='value' lines, optionally after export and with a # comment after; anything else "
                          "refuses), so its settings can't be told without running it")
        if m.group(1) in EXPANDED:
            raise Unclear(f"{_label(path)} line {n} sets {m.group(1)}, which the settings' values are read with, so "
                          "they can't be told without running it")
        vals[m.group(1)] = m.group(2)
    return vals


def _value(token, root, home):
    """A setting's value as bash would give it from its token (_ASSIGN's): a single-quoted one as written (pieces
    joined by \\' with a quote); otherwise with $HOME and $LAB_ROOT (braced or not) put in, and an unquoted leading ~
    as the home folder. None when it uses any other variable."""
    if token[:1] == "'":
        return token[1:-1].replace("'\\''", "'")
    quoted = token[:1] == '"'
    s = token[1:-1] if quoted else token
    if not quoted and (s == "~" or s.startswith("~/")):
        s = home + s[1:]
    known = {"HOME": home, "LAB_ROOT": root}
    if any(m.group(0).strip("${}") not in known for m in re.finditer(_REF, s)):
        return None
    return re.sub(_REF, lambda m: known[m.group(0).strip("${}")], s)


def _folder(vals, key, default, root, home):
    """A folder setting: DEFAULT when it's unset or empty (as scripts/lib.sh's ${KEY:-default}); None when its value
    can't be told (another variable) or isn't an absolute path."""
    if not vals.get(key) or vals[key] in ('""', "''"):
        return default
    v = _value(vals[key], root, home)
    if v == "":
        return default
    return v if v and v.startswith("/") else None


def _plain(vals, key, root, home, label):
    """A setting that isn't a folder (a name, a provider, a place), "" when it's unset; Unclear when its value can't be
    told."""
    if key not in vals:
        return ""
    v = _value(vals[key], root, home)
    if v is None:
        raise Unclear(f"{label}: {key} uses a variable this doesn't read ({vals[key]})")
    return v


def _name(vals, root, home, label, default):
    """The computer's name: DEFAULT when the settings don't set VM_NAME; Unclear when they set it to something that
    isn't a VM name (scripts/lib.sh refuses it too), never the default."""
    if "VM_NAME" not in vals:
        return default
    v = _plain(vals, "VM_NAME", root, home, label)
    if not _NAME.fullmatch(v):
        raise Unclear(f"{label}: VM_NAME isn't a VM name ({vals['VM_NAME']})")
    return v


def _selected_file(env, main, vms):
    """The settings file MCOMP_CONFIG names (ENV's) as scripts/lib.sh chooses it, or None when it names none. With
    MCOMP_VM set too, both must choose the same file (config/vms/NAME.env, else config/local.env when that's NAME's:
    MAIN, the main computer); Unclear when they don't, or when the file isn't there (scripts/lib.sh then acts on no
    computer)."""
    cfg = env.get("MCOMP_CONFIG") or ""
    if not cfg:
        return None
    if not os.path.isfile(cfg):
        raise Unclear(f"MCOMP_CONFIG names {cfg}, which isn't a settings file (scripts/lib.sh then acts on none)")
    name = env.get("MCOMP_VM") or ""
    if name:
        mine = os.path.join(vms, name + ".env") if _NAME.fullmatch(name) else ""
        if mine and not os.path.isfile(mine):
            mine = main["file"] if os.path.isfile(main["file"]) and main["name"] == name else ""
        if not mine or os.path.realpath(mine) != os.path.realpath(cfg):
            raise Unclear(f"MCOMP_CONFIG ({cfg}) and MCOMP_VM ({name}) choose different settings")
    return cfg


def computers(root=None, home=None, env=None):
    """The computers a checkout (ROOT, this one by default) has settings for, each {name, file, run_dir, ssh_dir,
    provider, place}: its main one (config/local.env, else the defaults), one per config/vms/NAME.env, whose folders
    are fixed by its name (scripts/lib.sh mc_vm_default), and the one MCOMP_CONFIG chooses when that's another file
    (compared by real path; ENV's, this process's environment for this checkout and none for another), read as
    scripts/lib.sh reads it: the defaults, then that file, with MCOMP_VM naming the same file when it's set too. That
    one comes first: where two settings files give one name, it's the one this process acts on. Every file is read
    strictly (_parse_strict): one that is there but can't be read raises OSError, one that isn't plain assignments only
    raises Unclear, and so does an MCOMP_CONFIG that isn't a file, that MCOMP_VM disagrees with, that names a
    config/vms/ computer's name, or puts its computer elsewhere (only config/vms/ computers can be). What a gate uses
    this for refuses then, never reading them as none. A folder its settings give in a way this can't tell is None.
    What's missing is missing: no config/vms folder is no computers there, no file no settings in it."""
    root = os.path.abspath(root or LAB)
    home = home or os.environ.get("HOME") or os.path.expanduser("~")
    if env is None:
        env = os.environ if os.path.realpath(root) == os.path.realpath(LAB) else {}
    base = _parse_strict(os.path.join(root, "config", "defaults.env"), missing_ok=True)
    main_file = os.path.join(root, "config", "local.env")
    vms = os.path.join(root, "config", "vms")

    def kept(file, missing_ok=False):
        s = dict(base)
        s.update(_parse_strict(file, missing_ok=missing_ok))
        label = _label(file)
        return {"name": _name(s, root, home, label, DEFAULT_VM_NAME), "file": file,
                "run_dir": _folder(s, "RUN_DIR", os.path.join(root, ".run"), root, home),
                "ssh_dir": _folder(s, "SSH_DIR", os.path.join(home, ".ssh", "metacomputer"), root, home),
                "provider": _plain(s, "PROVIDER", root, home, label), "place": _plain(s, "PLACE", root, home, label)}

    out = [kept(main_file, missing_ok=True)]
    try:
        names = sorted(f[:-4] for f in os.listdir(vms) if f.endswith(".env") and _NAME.fullmatch(f[:-4]))
    except FileNotFoundError:
        names = []
    for n in names:
        f = os.path.join(vms, n + ".env")
        s = {k: v for k, v in base.items() if k not in IDENTITY}
        s.update(_parse_strict(f))
        out.append({"name": n, "file": f, "run_dir": os.path.join(root, ".run-vms", n),
                    "ssh_dir": os.path.join(home, ".ssh", "metacomputer-" + n),
                    "provider": _plain(s, "PROVIDER", root, home, _label(f)),
                    "place": _plain(s, "PLACE", root, home, _label(f))})
    cfg = _selected_file(env, out[0], vms)
    if cfg:
        real = os.path.realpath(cfg)
        vms_real = os.path.realpath(vms)
        if real == os.path.realpath(main_file):
            return out
        if os.path.dirname(real) == vms_real and real.endswith(".env"):
            if os.path.basename(real)[:-4] not in names:
                raise Unclear(f"MCOMP_CONFIG ({cfg}) is in config/vms/ but isn't a VM's settings file")
            return out
        c = kept(os.path.abspath(cfg))
        if c["name"] in names:
            raise Unclear(f"MCOMP_CONFIG ({cfg}) names the VM {c['name']}, which config/vms/{c['name']}.env is")
        if c["place"] not in ("", "mac"):
            raise Unclear(f"MCOMP_CONFIG ({cfg}) says PLACE={c['place']}: only a config/vms/ computer can be elsewhere")
        out.insert(0, c)
    return out


CONFIG = load()
GUEST_USER = CONFIG.get("GUEST_USER", "cua")
GUEST_HOME = os.path.join("/home", GUEST_USER)
GUEST_RUNS = os.path.join(GUEST_HOME, "metacomputer-runs")
