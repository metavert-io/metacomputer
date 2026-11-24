#!/usr/bin/env python3
"""metacomputer put: copy files from the Mac into the VM, checked.

    guest_put.py --transport N SSH-COMMAND... [--to GUEST_DIR] [--overwrite] HOST_PATH...

(bin/metacomputer passes the ops SSH command; N is how many of the arguments it takes.)

The transport is the one `put` always used: tar on the Mac (no Mac metadata or extended
attributes), over the ops SSH connection, into tar in the VM as the desktop user
(--no-same-owner). On top of it:

- everything lands first in a fresh hidden folder inside GUEST_DIR (.mcomp-put.XXXXXX);
- every regular file is hashed (SHA-256) on the Mac before it's sent and again in the VM
  after it arrived; any difference, a missing file or an extra one, and nothing is put in
  place (the hidden folder is removed);
- then each item is renamed into GUEST_DIR. A name that's taken is never overwritten: the
  copy lands as "name (1).ext", "name (2).ext", ... (atomically: renameat2 RENAME_NOREPLACE).
  --overwrite replaces the existing file or folder instead.

Prints each item's final path in the VM, one per line. The VM's answers are checked and
bounded, and text from it is escaped before it reaches a terminal.
"""
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boundedrun  # noqa: E402
from termsafe import escape_for_terminal as esc  # noqa: E402

GUEST_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+,:= -]+$")
MAX_REPLY = 32 << 20             # the hashes of a large folder fit; a VM can't make the Mac hold more
MAX_ERR = 64 << 10

# Runs in the VM (python3 -I -c): SHA-256 of every regular file under a folder, as JSON.
HASH_SCRIPT = r'''
import hashlib, json, os, stat, sys
root = sys.argv[1]
out = {}
for d, dirs, fs in os.walk(root):
    for n in fs:
        p = os.path.join(d, n)
        if stat.S_ISREG(os.lstat(p).st_mode):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[os.path.relpath(p, root)] = h.hexdigest()
print(json.dumps(out))
'''

# Runs in the VM: move each item from the staging folder into place; never overwrite unless asked.
MOVE_SCRIPT = r'''
import ctypes, json, os, shutil, sys

def candidates(name, is_dir):
    yield name
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    n = 1
    while True:
        yield f"{stem} ({n}){ext}"
        n += 1

def rename_noreplace(a, b):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.renameat2(-100, a.encode("utf-8", "surrogateescape"), -100, b.encode("utf-8", "surrogateescape"), 1) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), b)

def put(tmp, dest, overwrite, names):
    out = []
    for name in names:
        src = os.path.join(tmp, name)
        is_dir = os.path.isdir(src) and not os.path.islink(src)
        if overwrite:
            target = os.path.join(dest, name)
            if os.path.lexists(target):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.unlink(target)
            os.rename(src, target)
            out.append({"name": name, "dest": target, "renamed": False})
            continue
        for cand in candidates(name, is_dir):
            target = os.path.join(dest, cand)
            try:
                rename_noreplace(src, target)
            except FileExistsError:
                continue
            out.append({"name": name, "dest": target, "renamed": cand != name})
            break
    return out

if __name__ == "__main__":
    tmp, dest, overwrite = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
    try:
        print(json.dumps({"ok": True, "items": put(tmp, dest, overwrite, sys.argv[4:])}))
    except OSError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
'''


class PutError(Exception):
    pass


def host_hashes(path):
    """{relative name as it lands in the staging folder: SHA-256} for every regular file of path
    (links aren't followed and aren't hashed: tar sends them as links)."""
    base = os.path.basename(os.path.normpath(path))
    out = {}
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode):
        out[base] = sha256_file(path)
    elif stat.S_ISDIR(st.st_mode):
        for d, dirs, fs in os.walk(path):
            for n in fs:
                p = os.path.join(d, n)
                if not stat.S_ISREG(os.lstat(p).st_mode):
                    continue
                out[os.path.join(base, os.path.relpath(p, path))] = sha256_file(p)
    return out


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compare(host, guest):
    """Differences between the Mac's hashes and the VM's, as readable lines ([] when they match)."""
    if not isinstance(guest, dict):
        return ["the VM didn't send its hashes"]
    problems = []
    for name, h in sorted(host.items()):
        g = guest.get(name)
        if g is None:
            problems.append(f"missing in the VM: {name}")
        elif g != h:
            problems.append(f"different in the VM (changed while it was copied?): {name}")
    for name in sorted(set(guest) - set(host)):
        problems.append(f"in the VM but not sent: {name}")
    return problems


def guest_dir(p, user):
    home = f"/home/{user}"
    if p == "~":
        p = home
    elif p.startswith("~/"):
        p = home + p[1:]
    elif not p.startswith("/"):
        p = f"{home}/{p}"
    if not GUEST_PATH_RE.match(p):
        raise PutError(f"unsupported characters in guest path: {p}")
    return p.rstrip("/") or "/"


def remote(ssh, cmd, stdin=None, timeout=600):
    """One command in the VM. Its answer is untrusted: read as it comes, at most MAX_REPLY bytes (MAX_ERR of
    stderr), within timeout seconds; past either the connection is ended and PutError raised."""
    try:
        rc, out, err = boundedrun.run(ssh + [cmd], stdin=stdin, max_out=MAX_REPLY, max_err=MAX_ERR, timeout=timeout)
    except boundedrun.Overflow:
        raise PutError(f"the VM sent more than {MAX_REPLY >> 20} MB in answer; the copy was stopped")
    except boundedrun.Timeout:
        raise PutError(f"the VM didn't answer within {timeout} s; the copy was stopped")
    return rc, out.decode("utf-8", "replace"), err[:4096].decode("utf-8", "replace")


def last_json(text):
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def put(ssh, files, to, overwrite, user):
    dest = guest_dir(to, user)
    for f in files:
        if not os.path.lexists(f):
            raise PutError(f"no such file: {f}")
    hashes = {}
    names = []
    for f in files:
        base = os.path.basename(os.path.normpath(f))
        if base in names:
            raise PutError(f"two items named {base}: put them one at a time")
        names.append(base)
        hashes.update(host_hashes(f))
    rc, out, err = remote(ssh, f"mkdir -p -- {shlex.quote(dest)} && mktemp -d {shlex.quote(dest + '/.mcomp-put.XXXXXX')}")
    tmp = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    if not tmp or os.path.dirname(tmp) != dest or not re.fullmatch(r"\.mcomp-put\.[A-Za-z0-9]{6}", os.path.basename(tmp)):
        raise PutError(f"cannot create {dest} in the guest ({esc(err.strip()[:200])})")
    done = False
    try:
        for f in files:
            src = os.path.abspath(f)
            tar = subprocess.Popen(["tar", "--no-mac-metadata", "--no-xattrs", "-C", os.path.dirname(src) or "/",
                                    "-cf", "-", os.path.basename(src)], stdout=subprocess.PIPE,
                                   env=dict(os.environ, COPYFILE_DISABLE="1"))
            rc, _, err = remote(ssh, f"tar -C {shlex.quote(tmp)} --no-same-owner -xf -", stdin=tar.stdout)
            tar.stdout.close()
            if tar.wait() != 0 or rc != 0:
                raise PutError(f"copy failed: {f}" + (f" ({esc(err.strip()[:200])})" if err.strip() else ""))
        rc, out, err = remote(ssh, f"python3 -I -c {shlex.quote(HASH_SCRIPT)} {shlex.quote(tmp)}")
        problems = compare(hashes, last_json(out) if rc == 0 else None)
        if problems:
            raise PutError("the copy in the VM doesn't match (nothing was put in place):\n  "
                           + "\n  ".join(esc(p) for p in problems[:20]))
        rc, out, err = remote(ssh, f"python3 -I -c {shlex.quote(MOVE_SCRIPT)} {shlex.quote(tmp)} {shlex.quote(dest)} "
                                   f"{'1' if overwrite else '0'} " + " ".join(shlex.quote(n) for n in names))
        res = last_json(out)
        done = True                  # the move script removes the staging folder either way
        if not isinstance(res, dict) or res.get("ok") is not True or not isinstance(res.get("items"), list):
            why = res.get("error") if isinstance(res, dict) else err.strip()[:200]
            raise PutError(f"could not put the files in place in {dest}: {esc(str(why))}")
        return res["items"], len(hashes)
    finally:
        if not done:
            try:
                remote(ssh, f"rm -rf -- {shlex.quote(tmp)}", timeout=60)
            except PutError:
                pass


def main(argv):
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if len(argv) < 2 or argv[0] != "--transport" or not argv[1].isdigit():
        print(__doc__, file=sys.stderr)
        return 2
    n = int(argv[1])
    ssh, rest = argv[2:2 + n], argv[2 + n:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # what follows the transport, by metacomputer's rules, before anything is copied
    strictargs.expect(rest, ["PATH... [--to GUEST_DIR] [--overwrite]"], prog="metacomputer put",
                      usage="usage: metacomputer put HOST_PATH... [--to GUEST_DIR] [--overwrite]")
    to, overwrite, files, to_given = "~/Inbox", False, [], False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--to":
            if to_given:                        # one folder: a second --to is refused, never let through
                print("metacomputer: --to given twice (one folder, please)", file=sys.stderr)
                return 2
            to_given = True
            if i + 1 >= len(rest):
                print("metacomputer: --to needs a folder in the VM", file=sys.stderr)
                return 2
            to = rest[i + 1]
            i += 2
            continue
        if a == "--overwrite":
            if overwrite:
                print("metacomputer: --overwrite given twice", file=sys.stderr)
                return 2
            overwrite = True
        elif a in ("-h", "--help"):        # usage only, never a file to copy
            print(__doc__)
            return 0
        elif a.startswith("-") and a != "-":    # an option it doesn't have, never a file to copy (./-name is one)
            print(f"metacomputer: unknown option {a[:60]!r}; usage: metacomputer put HOST_PATH... [--to GUEST_DIR] "
                  "[--overwrite]", file=sys.stderr)
            return 2
        else:
            files.append(a)
        i += 1
    if not files:
        print("usage: metacomputer put HOST_PATH... [--to GUEST_DIR] [--overwrite]", file=sys.stderr)
        return 2
    # It writes in the guest: while the user has the computer (a handoff) it waits (lib/handoff_gate.py): exit 4 before
    # anything is read or sent, except at the user's own terminal or under the hold of the command that runs this
    # (bin/metacomputer put).
    # The computer is the one its transport reaches (the SSH folder it names), and a run folder given must be that
    # computer's (handoff_gate.owners_dir_for): never the default folder for another computer.
    import handoff_gate
    handoff_gate.wait_or_hold(handoff_gate.owners_dir_for(name=os.environ.get("MCOMP_VM_NAME") or None,
                                                          run_dir=os.environ.get("MCOMP_RUN_DIR"), transport=ssh))
    user = os.environ.get("MCOMP_GUEST_USER") or "cua"
    try:
        items, nfiles = put(ssh, files, to, overwrite, user)
    except PutError as e:
        print(f"metacomputer: {e}", file=sys.stderr)
        return 1
    for it in items:
        dest = str(it.get("dest", ""))
        print(esc(dest))
        if it.get("renamed"):
            print(f"  (a {esc(it.get('name'))} already exists there; this copy is {esc(os.path.basename(dest))}; "
                  "--overwrite replaces instead)", file=sys.stderr)
    print(f"  ✓ {nfiles} file{'s' if nfiles != 1 else ''} verified (SHA-256)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
