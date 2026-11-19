#!/usr/bin/env python3
"""The one way metacomputer deletes a folder on the Mac (what rm -rf and shutil.rmtree would do), and only when that
folder, as the folder it physically is, neither is nor holds a folder some VM of this checkout uses.

    safedelete.py rmtree [--quiet] [--] PATH...   delete each PATH: a folder with everything in it, a file or a link
                                                  itself (never what a link points to); a PATH that isn't there is
                                                  fine. Status 1, with the reason, when one is refused.
    safedelete.py check [--] PATH...              only the check
    scripts/safe-rmtree.sh PATH...                the same as rmtree, for shell scripts
    Python: import safedelete; safedelete.rmtree(path, ...)

The folders no delete may be or hold are every VM's SSH_DIR, RUN_DIR, LOG_DIR, VAULT_DIR and EVENTS_SPOOL (the main
VM's, every config/vms/ VM's, the MCOMP_CONFIG file's, the account's own vault and spools, and a vault folder or spool
the environment names; scripts/lib.sh mc_all_identity_records), the account's home and this checkout. Each is compared
as the folder it physically is (lib/vms.py physical: every link at every level followed, then device and inode), so a
link anywhere above the folder to delete, or above a VM's folder, can't hide one in the other. It fails closed: when a
settings file can't be read, or a VM's folder can't be resolved, nothing is deleted.

Inside a folder it deletes, links are removed, never followed (shutil.rmtree), so what they point to is kept. The
runner's own delete of a VM (its storage folder) is the one recursive delete not made here: it runs after the identity
rule (scripts/lib.sh vm_change_lock, vm delete's plan, build-vm.sh's check)."""
import io
import os
import pwd
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import vms  # noqa: E402


Refused = vms.Refused          # one refusal for the identity rule and for deletes


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


def protected(home=None, lab=LAB, owners=None):
    """[(what, value, identity, chain, links)]: every folder no delete may be or hold, with the links on the way to it
    (links_on_the_way). Fails closed (Refused)."""
    home = home or account_home()
    owners = records(lab) if owners is None else owners
    out = []

    def add(what, v):
        try:
            out.append((what, v, *vms.physical(v, home, what), links_on_the_way(v, home, what)))
        except vms.Refused as e:
            raise Refused(f"{e}; so nothing is deleted that might be it")
    for owner, ids in owners.items():
        if ids.get("UNREADABLE"):
            raise Refused(f"{owner} can't be read, so its folders aren't known and nothing is deleted that might be "
                          "one of them; fix it first")
        for k in vms.FOLDER_KEYS:
            for v in ids.get(k, []):
                add(f"{owner}'s {vms.shown(k)}", v)
    for what, v in (("this account's home folder", home), ("the home folder", os.environ.get("HOME") or home),
                    ("this checkout", lab)):
        add(what, v)
    return out


def check(path, prot, home=None):
    """Refused unless deleting path takes nothing in prot: a folder (or file), as it physically is, may neither be nor
    hold a protected folder, nor hold a link on the way to one; a link (deleted itself, never what it points to) may
    not be a link on the way to one."""
    home = home or account_home()
    target = os.path.abspath(path)
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode):
        where = vms.physical(os.path.dirname(target), home, f"the folder of {path}")[0]
        for what, v, _pid, _pchain, links in prot:
            if any(lid == where and name == os.path.basename(target).casefold() for lid, name, _l in links):
                raise Refused(f"{path} is a link on the way to {what} ({v}); it wasn't deleted")
        return
    ident, _chain = vms.physical(target, home, f"the folder to delete ({path})")
    for what, v, pid, pchain, links in prot:
        if pid == ident:
            raise Refused(f"{path} is {what} ({v}); it wasn't deleted")
        if ident in pchain:
            raise Refused(f"{path} holds {what} ({v}); it wasn't deleted")
        for lid, _name, lpath in links:
            if lid == ident or ident in vms.physical(lpath.rsplit("/", 1)[0] or "/", home, what)[1]:
                raise Refused(f"{path} holds {lpath}, a link on the way to {what} ({v}); it wasn't deleted")


def rmtree(*paths, quiet=False, lab=LAB, out=sys.stderr):
    """Delete each path (rm -rf), each only after check(). A refusal raises Refused (quiet: it's said on out and the
    path is kept; the rest go on), and so does a failure to delete (quiet: said and kept, as with ignore_errors)."""
    home = account_home()
    prot = None
    for path in paths:
        if not path or not os.path.lexists(path):
            continue
        try:
            if prot is None:
                prot = protected(home, lab)
            check(path, prot, home)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(os.path.realpath(path))
            else:
                os.unlink(path)              # a file or a link: itself, never what a link points to
        except Refused as e:
            if not quiet:
                raise
            print(f"metacomputer: not deleting {path}: {e}", file=out)
        except OSError as e:
            if not quiet:
                raise
            print(f"metacomputer: couldn't delete {path}: {e.strerror}", file=out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("rmtree", "check"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    quiet = "--quiet" in args[:2]
    args = [a for a in args if a != "--quiet"]
    if args[:1] == ["--"]:
        args = args[1:]
    try:
        if cmd == "check":
            home = account_home()
            prot = protected(home)
            for p in args:
                if os.path.lexists(p):
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
