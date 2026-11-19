#!/usr/bin/env python3
"""The one way metacomputer deletes a folder on the Mac (what rm -rf, shutil.rmtree or a pruning walk would do), and
only when that folder, as the folder it physically is, neither is nor holds a folder some VM of this checkout uses.

    safedelete.py rmtree [--quiet] [--] PATH...   delete each PATH: a folder with everything in it, a file or a link
                                                  itself (never what a link points to); a PATH that isn't there is
                                                  fine. Status 1, with the reason, when one is refused.
    safedelete.py prune --older-than SECONDS [--] FOLDER...
                                                  delete the regular files older than that anywhere in each FOLDER
    safedelete.py check [--] PATH...              only the check
    safedelete.py own --base DIR --rel A/NAME --target-vm NAME [--own PATH]... [--check]
                                                  vm delete's delete of that VM's own folder (prints present|absent)
    scripts/safe-rmtree.sh PATH...                the same as rmtree, for shell scripts
    Python: import safedelete; safedelete.rmtree(path, ...), prune_files(folder, choose),
            remove_records(folder, names, pattern), rmtree_own(base, rel, target_vm, own)

A path is resolved once (Target): it may not end in /, /. or /.. (each means another entry than the one it names),
its folder is resolved physically (as the system resolves it: links, then .., at every level), and its last part is
taken as it is (a link there is a link: deleted itself, never what it points to). That resolved entry is what's
checked and exactly what's deleted, through a descriptor of its folder: a folder is opened without following a link,
checked to be the one checked, and emptied bottom-up through descriptors (remove_tree_at; not shutil.rmtree, whose
dir_fd needs Python 3.11, while macOS ships 3.9); one that changed in between isn't deleted.

The folders no delete may be or hold are every VM's SSH_DIR, RUN_DIR, LOG_DIR, VAULT_DIR and EVENTS_SPOOL (the main
VM's, every config/vms/ VM's, the MCOMP_CONFIG file's, and a vault folder or spool the environment names; scripts/lib.sh
mc_all_identity_records), the account's own vault folder and spools whatever any settings say, the account's home and
this checkout. Each is compared as the folder it physically is (lib/vms.py physical: every link at every level
followed, then device and inode), and a delete may not hold a link on the way to one either. It fails closed: when a
settings file can't be read, or a VM's folder can't be resolved, nothing is deleted.

A program's own records (claims, journals) go through remove_records: only names of its own pattern, in a folder that
isn't a link and is checked like any delete's target. vm delete's delete of a VM's own folders goes through
rmtree_own: the same protected folders (protected(), the one list), less that VM's own.

The runner's own delete of a VM (its storage folder) is the one recursive delete on the Mac not made here: it runs after
the identity rule (scripts/lib.sh vm_change_lock, vm delete's plan, build-vm.sh's check)."""
import io
import os
import pwd
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import vms  # noqa: E402


Refused = vms.Refused          # one refusal for the identity rule and for deletes
ACCOUNT_VAULT = os.path.join("Library", "Application Support", "metacomputer")   # under the account's home


def account_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


def records(lab=LAB):
    """{owner: {key: [values]}} for every VM this checkout knows (scripts/lib.sh mc_all_identity_records), read in a
    shell that only defines lib.sh's functions (no settings loaded, nothing made)."""
    try:
        p = subprocess.run(["/bin/bash", "-c", 'MC_LIB_DEFS_ONLY=1 . "$1/scripts/lib.sh" && mc_all_identity_records',
                            "safedelete", lab], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"couldn't read this checkout's VMs ({e}), so nothing is deleted")
    if p.returncode != 0 or not p.stdout:
        why = p.stderr.decode("utf-8", "replace").strip()[:200]
        raise Refused(f"couldn't read this checkout's VMs ({why or 'no answer'}), so nothing is deleted")
    return vms.read_records(io.StringIO(p.stdout.decode("utf-8", "surrogateescape")))


def links_on_the_way(path, home, what, limit=64):
    """[(identity of the folder a link is in, its name casefolded, the link's path)] for every symbolic link met while
    path is resolved, one part at a time (where each sits, physically): deleting one of them, or a folder holding one,
    would take path away even though what path names lives elsewhere."""
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    out, todo, cur, hops = [], [c for c in path.split("/") if c], "/", 0
    while todo:
        c = todo.pop(0)
        if c == ".":
            continue
        if c == "..":
            cur = os.path.dirname(cur) or "/"
            continue
        nxt = os.path.join(cur, c)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            return out                      # the rest doesn't exist yet: no more links on the way
        except OSError as e:
            raise Refused(f"can't read {what} ({path}): {e.strerror}")
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > limit:
                raise Refused(f"can't read {what} ({path}): too many links")
            out.append((vms.physical(cur, home, what)[0], c.casefold(), nxt))
            tgt = os.readlink(nxt)
            todo = [x for x in tgt.split("/") if x] + todo
            if tgt.startswith("/"):
                cur = "/"
        else:
            cur = nxt
    return out


def protected(home=None, lab=LAB, owners=None, exclude_vm=None, own=()):
    """[(what, value, identity, chain, links)]: every folder no delete may be or hold, with the links on the way to it
    (links_on_the_way). Fails closed (Refused). The one list: every delete here, vm delete's included, uses it.
    exclude_vm (vm delete): that VM's own folders are left out (they're what it deletes), so are the environment's
    vault folder and spool when they're its own folders (own); the account's own vault and spools, the home folder and
    the checkout never are."""
    home = home or account_home()
    owners = records(lab) if owners is None else owners
    out = []
    mine = {os.path.normpath(v) for v in own if isinstance(v, str) and v}

    def add(what, v):
        try:
            out.append((what, v, *vms.physical(v, home, what), links_on_the_way(v, home, what)))
        except vms.Refused as e:
            raise Refused(f"{e}; so nothing is deleted that might be it")
    for owner, ids in owners.items():
        if exclude_vm and not ids.get("UNREADABLE"):
            if ids.get("VM_NAME") == [exclude_vm]:
                continue
            vals = [v for k in vms.FOLDER_KEYS for v in ids.get(k, [])]
            if owner.startswith("the environment (") and vals and all(os.path.normpath(v) in mine for v in vals):
                continue
        if ids.get("UNREADABLE"):
            raise Refused(f"{owner} can't be read, so its folders aren't known and nothing is deleted that might be "
                          "one of them; fix it first")
        for k in vms.FOLDER_KEYS:
            for v in ids.get(k, []):
                add(f"{owner}'s {vms.shown(k)}", v)
    env_home = os.environ.get("HOME") or home
    for what, v in (("this account's home folder", home), ("the home folder", env_home), ("this checkout", lab),
                    # The account's own vault folder and spools, whatever any settings file says (a config that names
                    # its own still leaves these where the vault's defaults put them).
                    ("the account's vault folder", os.path.join(home, ACCOUNT_VAULT)),
                    ("the account's event spool", os.path.join(home, ACCOUNT_VAULT, "events", "spool")),
                    ("the vault folder of $HOME", os.path.join(env_home, ACCOUNT_VAULT)),
                    ("the event spool of $HOME", os.path.join(env_home, ACCOUNT_VAULT, "events", "spool"))):
        add(what, v)
    return out


class Target:
    """What a path to delete is, resolved once: the physical folder it's in (parent: no links left in it, as
    realpath gives it), its last part (name) and that part's lstat (st, None when it isn't there). Everything after
    works on exactly this: the check, and the delete through a descriptor of parent."""

    def __init__(self, path):
        if not isinstance(path, str) or not path:
            raise Refused("nothing to delete was named")
        if path.endswith("/"):
            raise Refused(f"{path}: a path to delete that ends in / asks for what a link there points to; name the "
                          "folder or the link itself")
        a = path if path.startswith("/") else os.path.join(os.getcwd(), path)
        parts = [c for c in a.split("/") if c]
        if not parts:
            raise Refused("not deleting /")
        if parts[-1] in (".", ".."):
            raise Refused(f"{path}: a path to delete can't end in . or .. (it means a folder it doesn't name); name "
                          "the folder itself")
        self.spelled = path
        self.parent = os.path.realpath("/" + "/".join(parts[:-1]))
        self.name = parts[-1]
        self.path = os.path.join(self.parent, self.name)
        try:
            self.st = os.lstat(self.path)
        except FileNotFoundError:
            self.st = None

    @property
    def is_link(self):
        return self.st is not None and stat.S_ISLNK(self.st.st_mode)

    @property
    def is_dir(self):
        return self.st is not None and stat.S_ISDIR(self.st.st_mode)


def check_target(t, prot, home):
    """Refused unless deleting t takes nothing in prot. A folder (or file), as it physically is, may neither be nor
    hold a protected folder, nor hold a link on the way to one; a link (deleted itself, never what it points to) may
    not be a link on the way to one."""
    if t.st is None:
        return
    if t.is_link:
        where = vms.physical(t.parent, home, f"the folder of {t.spelled}")[0]
        for what, v, _pid, _pchain, links in prot:
            if any(lid == where and name == t.name.casefold() for lid, name, _l in links):
                raise Refused(f"{t.spelled} is a link on the way to {what} ({v}); it wasn't deleted")
        return
    ident, _chain = vms.physical(t.path, home, f"the folder to delete ({t.spelled})")
    for what, v, pid, pchain, links in prot:
        if pid == ident:
            raise Refused(f"{t.spelled} is {what} ({v}); it wasn't deleted")
        if ident in pchain:
            raise Refused(f"{t.spelled} holds {what} ({v}); it wasn't deleted")
        for lid, _name, lpath in links:
            if lid == ident or ident in vms.physical(lpath.rsplit("/", 1)[0] or "/", home, what)[1]:
                raise Refused(f"{t.spelled} holds {lpath}, a link on the way to {what} ({v}); it wasn't deleted")


def check(path, prot, home=None):
    """check_target for a path (resolved once, as rmtree resolves it)."""
    check_target(Target(path), prot, home or account_home())


def _open_parent(t):
    """A descriptor of t's folder, and t's entry in it still being what was checked."""
    fd = os.open(t.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        now = os.stat(t.name, dir_fd=fd, follow_symlinks=False)
        if (now.st_dev, now.st_ino, stat.S_IFMT(now.st_mode)) != (t.st.st_dev, t.st.st_ino, stat.S_IFMT(t.st.st_mode)):
            raise Refused(f"{t.spelled} changed while it was being checked; it wasn't deleted")
        return fd
    except BaseException:
        os.close(fd)
        raise


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def fd_ops_ok():
    """Whether this Python deletes through folder descriptors here (every macOS Python 3 does)."""
    return (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and os.unlink in os.supports_dir_fd
            and os.rmdir in os.supports_dir_fd and os.scandir in os.supports_fd)


def _open_dir_at(name, dir_fd, expect=None, what=None):
    """name in dir_fd opened as a folder, never through a link; refused when it isn't the folder expect (an lstat)
    says it was."""
    fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    if expect is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            os.close(fd)
            raise Refused(f"{what or name} changed while it was being deleted; what's left of it was kept")
    return fd


def remove_tree_at(parent_fd, name, expect, what=None):
    """Delete the folder name in parent_fd, with everything in it, through descriptors only: each folder is opened
    without following a link and checked to be the one just listed (expect: the top's lstat), its files and links are
    unlinked in it, its folders emptied the same way, then removed. No link is ever followed, and shutil.rmtree isn't
    used (its dir_fd needs Python 3.11; macOS's own python3 is 3.9)."""
    stack = [(_open_dir_at(name, parent_fd, expect, what), name)]
    try:
        while stack:
            fd = stack[-1][0]
            with os.scandir(fd) as it:
                names = [e.name for e in it]
            down = None
            for n in names:
                st = os.stat(n, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    down = (_open_dir_at(n, fd, st, what), n)
                    break
                os.unlink(n, dir_fd=fd)
            if down is not None:
                stack.append(down)
                continue
            fd, n = stack.pop()
            os.close(fd)
            os.rmdir(n, dir_fd=stack[-1][0] if stack else parent_fd)
    finally:
        for fd, _n in stack:
            os.close(fd)


def _delete(t):
    """Delete exactly the checked entry, through its folder's descriptor: a folder with everything in it
    (remove_tree_at, which follows no link), else the entry itself (a file, or a link, never what it points to)."""
    fd = _open_parent(t)
    try:
        if t.is_dir:
            remove_tree_at(fd, t.name, t.st, t.spelled)
        else:
            os.unlink(t.name, dir_fd=fd)
    finally:
        os.close(fd)


def rmtree(*paths, quiet=False, lab=LAB, out=sys.stderr):
    """Delete each path (rm -rf), each resolved once (Target), checked (check_target) and deleted exactly as resolved.
    A refusal raises Refused (quiet: it's said on out and the path is kept; the rest go on), and so does a failure to
    delete (quiet: said and kept, as with ignore_errors)."""
    home = account_home()
    prot = None
    ops = fd_ops_ok()
    for path in paths:
        try:
            if not ops:
                raise Refused("this Python can't delete through folder descriptors, so nothing is deleted with it")
            t = Target(path)
            if t.st is None:
                continue
            if prot is None:
                prot = protected(home, lab)
            check_target(t, prot, home)
            _delete(t)
        except Refused as e:
            if not quiet:
                raise
            print(f"metacomputer: not deleting {path}: {e}", file=out)
        except OSError as e:
            if not quiet:
                raise
            print(f"metacomputer: couldn't delete {path}: {e.strerror}", file=out)
        except Exception as e:                  # quiet (a cleanup): never a crash; the path is kept
            if not quiet:
                raise
            print(f"metacomputer: couldn't delete {path} ({type(e).__name__}); it was kept", file=out)


def prune_files(root, choose, lab=LAB):
    """A delete that walks a tree: root is resolved and checked like rmtree's target (a link there is refused: it's
    the folder itself that's pruned), then walked through directory descriptors, never following a link, and the
    regular files choose([(mtime, size, relpath)]) returns are unlinked through their folder's descriptor. Returns
    how many were deleted. A refusal raises Refused before anything is deleted."""
    home = account_home()
    t = Target(root)
    if t.st is None:
        return 0
    if not t.is_dir:
        raise Refused(f"{root} isn't a folder (a link isn't followed); nothing was deleted in it")
    check_target(t, protected(home, lab), home)
    files = []
    top = _open_parent(t)
    try:
        rfd = _open_dir_at(t.name, top, t.st, root)
    finally:
        os.close(top)
    try:
        for dpath, _dirs, fnames, dfd in os.fwalk(".", dir_fd=rfd, follow_symlinks=False):
            for f in fnames:
                try:
                    st = os.stat(f, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    files.append((st.st_mtime, st.st_size, os.path.normpath(os.path.join(dpath, f))))
        n = 0
        for rel in choose(files):
            parts = rel.split("/")
            if any(c in ("", ".", "..") for c in parts):
                continue
            fd, opened = rfd, []
            try:
                for c in parts[:-1]:          # each folder on the way opened without following a link
                    fd = _open_dir_at(c, fd)
                    opened.append(fd)
                st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(parts[-1], dir_fd=fd)
                    n += 1
            except OSError:
                pass
            finally:
                for x in opened:
                    os.close(x)
        return n
    finally:
        os.close(rfd)


def remove_records(folder, names, pattern, lab=LAB):
    """Delete some of a program's own records (stale claims, broken journal entries): the regular files among names in
    folder whose whole name matches pattern (a compiled regex), nothing else. folder is resolved and checked like any
    delete's target: not a link, and neither being, holding nor reaching (through a link in it) a protected folder.
    It's opened without following a link and each file is unlinked through its descriptor. Returns how many went; a
    refusal raises Refused before any is deleted."""
    names = [n for n in names if isinstance(n, str) and "/" not in n and pattern.fullmatch(n)]
    if not names:
        return 0
    home = account_home()
    t = Target(folder)
    if t.st is None:
        return 0
    if not t.is_dir:
        raise Refused(f"{folder} isn't a folder (a link isn't followed); no record in it was deleted")
    check_target(t, protected(home, lab), home)
    top = _open_parent(t)
    try:
        dfd = _open_dir_at(t.name, top, t.st, folder)
    finally:
        os.close(top)
    n = 0
    try:
        for name in names:
            try:
                if stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                    os.unlink(name, dir_fd=dfd)
                    n += 1
            except OSError:
                pass
        return n
    finally:
        os.close(dfd)


def rmtree_own(base, rel, target_vm, own=(), check_only=False, lab=LAB):
    """vm delete's delete of target_vm's own folder base/rel (its runtime folder or vault): base is walked from / one
    folder at a time without following a link, then each part of rel (a link anywhere refuses); the folder must be
    this account's and is checked like every other delete (check_target) against protected() less target_vm's own
    folders (exclude_vm, own). Deleted through descriptors (remove_tree_at). False when it isn't there."""
    home = account_home()
    if not fd_ops_ok():
        raise Refused("this Python can't delete through folder descriptors, so nothing is deleted with it")
    parts = [c for c in vms.absolute(base, home, "the base folder").split("/") if c]
    relparts = [c for c in rel.split("/") if c]
    if not relparts or any(c in (".", "..") for c in relparts):
        raise Refused(f"not a folder name: {rel}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        walked = "/"
        for c in parts + relparts[:-1]:
            try:
                nfd = _open_dir_at(c, fd)
            except FileNotFoundError:
                return False
            except OSError:
                raise Refused(f"{os.path.join(walked, c)} is a link or not a folder; nothing there was deleted")
            os.close(fd)
            fd, walked = nfd, os.path.join(walked, c)
        leaf = relparts[-1]
        try:
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        target = os.path.join(walked, leaf)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{target} is a link or not a folder; it wasn't deleted")
        if st.st_uid != os.getuid():
            raise Refused(f"{target} isn't this account's; it wasn't deleted")
        t = Target(target)
        if t.st is None or (t.st.st_dev, t.st.st_ino) != (st.st_dev, st.st_ino):
            raise Refused(f"{target} changed while it was being checked; it wasn't deleted")
        check_target(t, protected(home, lab, exclude_vm=target_vm, own=own), home)
        if check_only:
            return True
        remove_tree_at(fd, leaf, st, target)
        return True
    finally:
        os.close(fd)


def _own_main(args):
    import argparse
    ap = argparse.ArgumentParser(prog="safedelete.py own")
    ap.add_argument("--base", required=True)
    ap.add_argument("--rel", required=True)
    ap.add_argument("--target-vm", required=True)
    ap.add_argument("--own", action="append", default=[])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(args)
    try:
        done = rmtree_own(a.base, a.rel, a.target_vm, a.own, check_only=a.check)
    except Refused as e:
        print(str(e))
        return 1
    except OSError as e:
        print(f"couldn't delete {os.path.join(a.base, a.rel)}: {e.strerror}")
        return 1
    print("present" if done else "absent")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("rmtree", "check", "prune", "own"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "own":
        return _own_main(args)
    quiet = args[:1] == ["--quiet"]
    if quiet:
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    older = None
    if cmd == "prune":
        if len(args) < 2 or args[0] != "--older-than" or not args[1].isdigit():
            print("usage: safedelete.py prune --older-than SECONDS [--] FOLDER...", file=sys.stderr)
            return 2
        older, args = int(args[1]), args[2:]
        if args[:1] == ["--"]:
            args = args[1:]
    try:
        if cmd == "prune":
            import time
            cut = time.time() - older
            for p in args:
                prune_files(p, lambda files: [rel for mtime, _size, rel in files if mtime < cut])
        elif cmd == "check":
            home = account_home()
            prot = protected(home)
            for p in args:
                check(p, prot, home)
        else:
            rmtree(*args, quiet=quiet)
    except Refused as e:
        print(f"metacomputer: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"metacomputer: couldn't delete: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
