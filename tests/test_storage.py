#!/usr/bin/env python3
"""Shared and persistent storage, on the Mac's side (lib/storage.py, lib/guest_put.py). No VM.

    python3 tests/test_storage.py

Everything runs in a temporary registry and data folder (MCOMP_STORAGE_HOME, MCOMP_STORAGE_DIR);
nothing of this Mac's own storage, config or Keychain is read or changed. It checks:
- VOLUMES / REFDISKS parsing: default and custom mount points, ro/rw, the guest paths refused
  (system folders, dot-folders, the home folder itself), the same rules in the guest's helper;
- the folders that may never go into the VM (personal folders and their parents, Library and
  iCloud Drive, keys, this checkout, system folders, the gateway's sockets), with links resolved;
- locks: one VM per volume, a live holder refuses, a dead one (pid gone or reused) doesn't, the
  same VM may take its own again, a lock file from another computer refuses, release;
- network filesystems: the statfs classifier, a local folder is local, a network one warns;
- the plan for a start: missing images refuse, read-only disk volumes refuse, a VM built before
  storage support refuses, bounded mode leaves out what the manifest doesn't list, the run
  arguments' order, and the guest's helper accepts the plan the Mac writes;
- sizes and names; sparse copies and checkpoints of volumes (save, change, restore);
- reference disks leave links out of what they pack;
- put: SHA-256 comparison (missing, changed, extra files) and the "name (1).ext" collision names;
- folder volumes: read-only by default, read-write only with .noindex in the path, refused
  folders, ':' refused, one VM read-write at a time (nested folders too), --shared-dir arguments
  and the share names the VM runner gives them;
- the folder watcher, on a real folder: a new file gets the quarantine attribute (and Gatekeeper
  then refuses to run an unsigned binary carrying it), a removed or rewritten attribute is put back,
  a link out of the folder becomes a note while one inside stays, a FIFO goes, set-id bits go, and
  what was there before the VM booted is left alone.
"""
import contextlib
import importlib.machinery
import io
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
FAILS = []
TMP = tempfile.mkdtemp(prefix="mcst-test-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None)
os.environ.update(MCOMP_STORAGE_HOME=os.path.join(TMP, "state"), MCOMP_STORAGE_DIR=os.path.join(TMP, "data"),
                  MCOMP_VM_NAME="testvm", MCOMP_RUN_DIR=os.path.join(TMP, "run"), MCOMP_GUEST_USER="cua",
                  MCOMP_SSH_DIR=os.path.join(TMP, "ssh"), MCOMP_VOLUMES="", MCOMP_REFDISKS="",
                  MCOMP_DRIVER_MODE="standard", MCOMP_DRIVER_MANIFEST="", MCOMP_EVENTS_DIR=os.path.join(TMP, "events"),
                  MCOMP_LOG_DIR=os.path.join(TMP, "logs"))
for d in ("run", "ssh", "logs"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
import storage  # noqa: E402
H = storage.guest_home()   # the desktop user's home in the VM
import guest_put  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except storage.StorageError as e:
        return str(e)
    return None


def helper():
    """The guest's storage-helper, loaded as a module (its libc is loaded only on first use)."""
    path = os.path.join(LAB, "provisioning", "guest", "rootfs", "opt", "metacomputer", "bin", "storage-helper")
    loader = importlib.machinery.SourceFileLoader("storage_helper", path)
    spec = importlib.util.spec_from_loader("storage_helper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Args:
    def __init__(self, **k):
        self.__dict__.update(k)


def test_parsing():
    p = storage.parse_list("work, cache:/volume docs:~/papers:rw", "volume", H)
    check("VOLUMES: the default mount point is ~/data/NAME in the VM",
          p[0] == {"name": "work", "mount": f"{H}/data/work", "mode": None}, p[0])
    check("VOLUMES: a custom mount point (/volume, as scripts for Cua Spaces expect)", p[1]["mount"] == "/volume", p[1])
    check("VOLUMES: ~ is the desktop user's home in the VM, and :rw is read", p[2] == {
        "name": "docs", "mount": f"{H}/papers", "mode": "rw"}, p[2])
    r = storage.parse_list("docs", "refdisk", H)
    check("REFDISKS: the default mount point is ~/ref/NAME", r[0]["mount"] == f"{H}/ref/docs", r)
    bad = {"/etc": "work:/etc", "/usr/local": "work:/usr/local", "a dot-folder": "work:~/.ssh",
           "the home folder itself": "work:~", "a relative path": "work:data", "..": "work:/mnt/../etc",
           "a bad name": "Work", "twice": "a,a", "two mount points": "a:/mnt/x:/mnt/y", "junk": "a:xyz",
           "/home of another user": "a:/home/someone/x", "/boot": "a:/boot", "/root": "a:/root",
           "/var": "a:/var/data", "/opt": "a:/opt/x"}
    for what, text in bad.items():
        check(f"VOLUMES refuses {what} ({text})", raises(storage.parse_list, text, "volume", H) is not None)
    for good in ("/mnt", "/mnt/shared", "/data/x", "/srv/www", "/workspace", "/volume/sub", f"{H}/data/x"):
        check(f"mount point allowed: {good}", storage.valid_mountpoint(good, H))
    h = helper()
    paths = ["/mnt", "/mnt/shared", "/volume", "/data/x", "/srv", "/workspace/a", f"{H}/data/x", f"{H}/x",
             "/etc", H, f"{H}/.ssh", f"{H}/.config/x", "/usr/local", "/", "/mnt/../etc",
             "/mnt//x", "mnt", "/mnt/a b", "/home/someone/x", "/tmp/x", "/run/x", "/proc/x", "/mnt/.hidden"]
    same = [p for p in paths if storage.valid_mountpoint(p, H) != h.valid_mountpoint(p, H)]
    check("the Mac and the VM's helper agree on every mount point", not same, same)


def fake_home():
    root = os.path.join(TMP, "fs")
    home = os.path.join(root, "Users", "u")
    for d in ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Library/Mobile Documents/com~apple~CloudDocs",
              "Library/Application Support/x", ".ssh", ".aws", "project/sub", "Documents/shared-with-vm",
              ".local/share/metacomputer/volumes/s3copy.noindex", "lab/sub", "keys"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    os.symlink(os.path.join(home, "Documents"), os.path.join(home, "docs-link"))
    os.symlink(os.path.join(home, "project"), os.path.join(home, "proj-link"))
    os.symlink(os.path.join(home, ".ssh"), os.path.join(home, "project", "sneaky"))
    return home


def test_refusals():
    home = fake_home()
    where = dict(home=home, lab=os.path.join(home, "lab"), ssh_dir=os.path.join(home, "keys"),
                 data=os.path.join(home, ".local", "share", "metacomputer"), state=os.path.join(TMP, "state"))
    refused = {
        "the home folder": home, "Documents": os.path.join(home, "Documents"), "Desktop": os.path.join(home, "Desktop"),
        "Downloads": os.path.join(home, "Downloads"), "Pictures": os.path.join(home, "Pictures"),
        "Movies": os.path.join(home, "Movies"), "Music": os.path.join(home, "Music"),
        "a parent of the home folder": os.path.dirname(home),
        "Library": os.path.join(home, "Library"), "inside Library": os.path.join(home, "Library", "Application Support", "x"),
        "iCloud Drive": os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs"),
        ".ssh": os.path.join(home, ".ssh"), "another dot-folder (.aws)": os.path.join(home, ".aws"),
        "SSH_DIR": os.path.join(home, "keys"), "the metacomputer checkout": os.path.join(home, "lab"),
        "inside the checkout": os.path.join(home, "lab", "sub"),
        "a link to Documents": os.path.join(home, "docs-link"),
        "a link (inside an allowed folder) to .ssh": os.path.join(home, "project", "sneaky"),
        "/": "/", "/Volumes": "/Volumes", "/tmp itself": "/tmp", "/System": "/System", "/usr": "/usr",
        "/Applications": "/Applications", "STORAGE_DIR itself": where["data"],
    }
    for what, p in refused.items():
        if os.path.isdir(p):
            check(f"refused: {what}", raises(storage.check_shareable, p, **where) is not None)
    allowed = {"a project folder": os.path.join(home, "project"), "a folder inside Documents":
               os.path.join(home, "Documents", "shared-with-vm"),
               "an S3 copy inside STORAGE_DIR/volumes": os.path.join(where["data"], "volumes", "s3copy.noindex")}
    for what, p in allowed.items():
        e = raises(storage.check_shareable, p, **where)
        check(f"allowed: {what}", e is None, e)
    check("refused (with this Mac's real settings): this metacomputer checkout", raises(storage.check_shareable, LAB))
    real = storage.check_shareable(os.path.join(home, "proj-link"), **where)
    check("a link to an allowed folder resolves to the real folder", real == os.path.realpath(os.path.join(home, "project")), real)
    sock = tempfile.mkdtemp(prefix="mcomp-x", dir="/tmp")
    try:
        check("refused: the gateway's socket folders (/tmp/mcomp-*)", raises(storage.check_shareable, sock, **where) is not None)
    finally:
        os.rmdir(sock)
    check("refused: a folder that doesn't exist", raises(storage.check_shareable, os.path.join(home, "nope"), **where))


def dead_pid():
    p = subprocess.Popen(["sleep", "30"])
    start = storage.proc_start(p.pid)
    p.send_signal(signal.SIGKILL)
    p.wait()
    return p.pid, start


def test_locks():
    img = os.path.join(TMP, "lockvol.img")
    open(img, "w").close()
    it = {"kind": "disk", "name": "lv", "path": img, "lock": storage.lock_key("disk", img)}
    me = os.getpid()
    w = storage.acquire([it], "vm-a", "/lab", me)
    check("a disk volume locks for its VM", len(w) == 1 and storage.holder_of(img, "disk") is not None)
    e = raises(storage.acquire, [it], "vm-b", "/lab", me)
    check("...and another VM is refused while the holder lives, naming the holder", e and "vm-a" in e, e)
    check("...the same VM may take it again (a restart)", raises(storage.acquire, [it], "vm-a", "/lab", me) is None)
    side = json.load(open(img + ".lock"))
    check("...with a lock file beside the image naming this computer", side.get("host") == storage.hostname(), side)
    n = storage.release_locks("vm-a", "/lab")
    check("release lets go of it and its lock file", n == 1 and storage.holder_of(img, "disk") is None
          and not os.path.exists(img + ".lock"))
    pid, start = dead_pid()
    rec = {"kind": "disk", "name": "lv", "path": img, "vm": "crashed", "lab": "/lab", "pid": pid, "pstart": start,
           "host": storage.hostname(), "since": "then", "state": "running"}
    storage.atomic_json(os.path.join(storage.locks_dir(), it["lock"] + ".json"), rec)
    check("a holder whose process is gone doesn't count (a VM that crashed)",
          raises(storage.acquire, [it], "vm-b", "/lab", me) is None)
    storage.release_locks("vm-b", "/lab")
    rec.update(pid=me, pstart="Mon Jan  1 00:00:00 2001")
    storage.atomic_json(os.path.join(storage.locks_dir(), it["lock"] + ".json"), rec)
    check("nor does one whose pid now belongs to another process (start time differs)",
          raises(storage.acquire, [it], "vm-b", "/lab", me) is None)
    storage.release_locks("vm-b", "/lab")
    storage.atomic_json(img + ".lock", {"host": "other-mac", "vm": "far", "since": "then"})
    e = raises(storage.acquire, [it], "vm-b", "/lab", me)
    check("a lock file from another computer refuses, and says how to clear it", e and "other-mac" in e and ".lock" in e, e)
    os.unlink(img + ".lock")
    storage.acquire([it], "vm-c", "/lab", me)
    child = subprocess.Popen(["sleep", "30"])
    try:
        n = storage.update_holder("vm-c", "/lab", child.pid)
        rec = json.load(open(os.path.join(storage.locks_dir(), it["lock"] + ".json")))
        check("after lume run starts, the VM's process holds the lock", n == 1 and rec["pid"] == child.pid
              and rec["state"] == "running", rec)
    finally:
        child.kill()
        child.wait()
    check("...and when that process is gone, the lock is free", storage.holder_of(img, "disk") is None)
    storage.release_locks("vm-c", "/lab")


def test_network():
    check("smbfs, nfs, afpfs and webdav count as network filesystems",
          all(storage.is_network(t, True) for t in ("smbfs", "nfs", "afpfs", "webdav")))
    check("...and so does anything not marked local", storage.is_network("apfs", False))
    check("apfs and exfat on a local disk don't", not storage.is_network("apfs", True) and not storage.is_network("exfat", True))
    t, mnt, local = storage.fs_info(TMP)
    check("statfs on a temporary folder: a local filesystem", local and t in ("apfs", "hfs"), (t, mnt, local))
    check("network_fs() is None for it", storage.network_fs(TMP) is None)
    real = storage.fs_info
    storage.fs_info = lambda p: ("smbfs", "/Volumes/nas", False)
    try:
        rec = make_volume("netvol")
        items, problems, notes = [], [], []
        it = storage.plan_volume(rec, {"name": "netvol", "mount": f"{H}/data/netvol", "mode": None}, problems, notes)
        check("a disk volume on a network filesystem is attached with a warning about drops and locks",
              it and not problems and any("network filesystem" in n and "locks" in n for n in notes), notes)
    finally:
        storage.fs_info = real


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


def make_volume(name, size="16M"):
    quiet(storage.cmd_volume_create, Args(name=name, size=size, dir=None))
    return storage.reg_load("volume", name)


def test_plan():
    os.environ["MCOMP_VOLUMES"] = "pv"
    check("start refuses an unknown volume", any("no volume named pv" in p for p in storage.build_plan()[1]))
    rec = make_volume("pv")
    items, problems, notes = storage.build_plan()
    check("a VM built before storage support is refused, with the rebuild and --no-volumes routes",
          any("build --replace" in p and "--no-volumes" in p for p in problems), problems)
    with open(os.path.join(TMP, "ssh", "guest-features"), "w") as f:
        f.write("storage=1\nbuild=x\n")
    items, problems, notes = storage.build_plan()
    check("a disk volume plans as --disk, read-write, locked", not problems and items and items[0]["rw"]
          and items[0]["lock"] and storage.lume_args(items) == ["--disk", rec["path"]], (problems, items))
    check("--no-volumes plans nothing", storage.build_plan(no_volumes=True) == ([], [], []))
    os.rename(rec["path"], rec["path"] + ".away")
    check("a missing image refuses the start (is its drive connected?)",
          any("missing" in p for p in storage.build_plan()[1]))
    os.rename(rec["path"] + ".away", rec["path"])
    os.environ["MCOMP_VOLUMES"] = "pv:ro"
    check("a disk volume can't be attached read-only (that's a reference disk)",
          any("reference disk" in p for p in storage.build_plan()[1]))
    src = os.path.join(TMP, "refsrc")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    open(os.path.join(src, "a.txt"), "w").write("a")
    open(os.path.join(src, "sub", "b.txt"), "w").write("b")
    os.symlink("/etc/passwd", os.path.join(src, "escape"))
    for n in ("r1", "r2"):
        quiet(storage.cmd_refdisk_create, Args(name=n, src=src, dir=None))
    r1 = storage.reg_load("refdisk", "r1")
    check("a reference disk is a read-only image, and leaves links out", os.stat(r1["path"]).st_mode & 0o222 == 0
          and r1["left_out"] == 1 and r1["files"] == 2, r1)
    os.environ.update(MCOMP_VOLUMES="pv:/volume", MCOMP_REFDISKS="r1, r2:/mnt/r2")
    items, problems, notes = storage.build_plan()
    args = storage.lume_args(items)
    check("run arguments: disks, then the first reference disk as --mount and the rest as --usb-storage", args == [
        "--disk", rec["path"], "--mount", r1["path"], "--usb-storage", storage.reg_load("refdisk", "r2")["path"]], args)
    plan = storage.guest_plan(items)
    h = helper()
    try:
        h.check_plan(plan, H)
        ok = True
    except h.Refused as e:
        ok = str(e)
    check("the VM's helper accepts the plan the Mac writes", ok is True, ok)
    check("...and refuses a plan with a system mount point", raises_helper(h, dict(plan, disks=[dict(
        plan["disks"][0], mount="/etc")])))
    check("...or overlapping mount points", raises_helper(h, dict(plan, refdisks=[dict(plan["refdisks"][0],
                                                                                         mount="/volume/x")])))
    os.environ["MCOMP_REFDISKS"] = "r1:rw"
    check("reference disks can't be read-write", any("read-only" in p for p in storage.build_plan()[1]))
    os.environ["MCOMP_REFDISKS"] = "r1:/volume/inside"
    check("overlapping mount points refuse the start", any("overlaps" in p for p in storage.build_plan()[1]))
    man = os.path.join(TMP, "manifest.yaml")
    with open(man, "w") as f:
        f.write("version: 3\nallow:\n  tools: [list_windows]\nresources:\n  files:\n    read:\n"
                "      - {dir: /mnt, recursive: true}\n    write:\n      - {dir: " + H + "/metacomputer-runs, recursive: true}\n"
                "  desktop:\n    display: true\n")
    os.environ.update(MCOMP_DRIVER_MODE="bounded", MCOMP_DRIVER_MANIFEST=man, MCOMP_VOLUMES="pv:/volume",
                      MCOMP_REFDISKS="r2:/mnt/r2")
    items, problems, notes = storage.build_plan()
    names = [i["name"] for i in items]
    check("bounded mode attaches only what the manifest lists (a read folder covers the read-only disk)",
          names == ["r2"] and any("bounded mode" in n and "pv" in n for n in notes), (names, notes))
    os.environ.update(MCOMP_DRIVER_MODE="standard", MCOMP_DRIVER_MANIFEST="", MCOMP_REFDISKS="")


def raises_helper(h, plan):
    try:
        h.check_plan(plan, H)
    except h.Refused:
        return True
    return False


def test_sizes_names():
    check("sizes: 20G is 20 GiB", storage.parse_size("20G") == 20 << 30)
    check("sizes round up to whole MiB", storage.parse_size("16.5M") == 17 << 20)
    for bad in ("1K", "abc", "-5G", "100T"):
        check(f"size refused: {bad}", raises(storage.parse_size, bad) is not None)
    for bad in ("Work", "-x", "a_b", "a" * 41, "", "../x"):
        check(f"name refused: {bad!r}", raises(storage.check_name, bad) is not None)


def test_copies_and_checkpoints():
    src = os.path.join(TMP, "sparse.img")
    with open(src, "wb") as f:
        f.truncate(64 << 20)
        f.seek(5 << 20)
        f.write(b"A" * 4096)
        f.seek(40 << 20)
        f.write(b"B" * 4096)
    dst = os.path.join(TMP, "sparse-copy.img")
    storage.sparse_copy(src, dst)
    same = open(src, "rb").read() == open(dst, "rb").read()
    check("a sparse copy has the same bytes and stays sparse", same and os.stat(dst).st_blocks * 512 < 8 << 20,
          os.stat(dst).st_blocks * 512)
    how = storage.clone_or_copy(src, os.path.join(TMP, "clone.img"))
    check("on APFS a snapshot is a clone", how == "clone", how)
    rec = storage.reg_load("volume", "pv")
    os.environ["MCOMP_VOLUMES"] = "pv"
    with open(rec["path"], "r+b") as f:
        f.write(b"before")
    quiet(storage.cmd_checkpoint, Args(op="save", name="c1"))
    with open(rec["path"], "r+b") as f:
        f.write(b"after!")
    check("the checkpoint lists its volume", storage.read_json(storage.ckpt_manifest("testvm", "c1"))["volumes"][0]["name"] == "pv")
    quiet(storage.cmd_checkpoint, Args(op="restore", name="c1"))
    check("restore puts the volume back as it was at the checkpoint", open(rec["path"], "rb").read(6) == b"before")
    img = rec["path"]
    storage.acquire([{"kind": "disk", "name": "pv", "path": img, "lock": storage.lock_key("disk", img)}],
                    "another-vm", "/lab", os.getpid())
    check("a checkpoint refuses while another VM holds the volume",
          raises(storage.cmd_checkpoint, Args(op="save", name="c2")) is not None)
    check("...and so does a restore", raises(storage.cmd_checkpoint, Args(op="restore", name="c1")) is not None)
    check("...and resize and delete", raises(storage.cmd_volume_resize, Args(name="pv", size="32M")) is not None
          and raises(storage.cmd_volume_delete, Args(name="pv", yes=True)) is not None)
    storage.release_locks("another-vm", "/lab")
    check("volumes only grow", raises(storage.cmd_volume_resize, Args(name="pv", size="8M")) is not None)
    quiet(storage.cmd_volume_resize, Args(name="pv", size="32M"))
    check("resize grows the image", os.stat(img).st_size == 32 << 20)
    storage.cmd_checkpoint(Args(op="delete", name="c1"))
    check("deleting the checkpoint deletes its snapshot", not os.path.exists(storage.snapshot_path(img, "testvm", "c1")))


def test_put():
    ns = {}
    exec(guest_put.MOVE_SCRIPT.split('if __name__ == "__main__":')[0], ns)
    names = list(zip(range(3), ns["candidates"]("report.pdf", False)))
    check("put: a taken name lands as 'name (1).ext', then (2)",
          [n for _, n in names] == ["report.pdf", "report (1).pdf", "report (2).pdf"], names)
    check("put: folders and dot-files keep their whole name", next(iter([n for i, n in zip(range(2), ns["candidates"](
        "my.folder", True))][1:])) == "my.folder (1)" and [n for i, n in zip(range(2), ns["candidates"](
            ".bashrc", False))][1] == ".bashrc (1)")
    d = os.path.join(TMP, "puttree")
    os.makedirs(os.path.join(d, "sub"))
    open(os.path.join(d, "a.txt"), "w").write("a")
    open(os.path.join(d, "sub", "b.txt"), "w").write("b")
    os.symlink("a.txt", os.path.join(d, "link"))
    h = guest_put.host_hashes(d)
    check("put hashes every regular file under the folder (links travel as links, unhashed)",
          sorted(h) == ["puttree/a.txt", "puttree/sub/b.txt"], h)
    check("matching hashes: no problems", guest_put.compare(h, dict(h)) == [])
    bad = dict(h)
    bad["puttree/a.txt"] = "0" * 64
    check("a changed file is caught", any("different" in p for p in guest_put.compare(h, bad)))
    miss = dict(h)
    del miss["puttree/sub/b.txt"]
    check("a missing file is caught", any("missing" in p for p in guest_put.compare(h, miss)))
    extra = dict(h, **{"puttree/evil": "1" * 64})
    check("an extra file is caught", any("not sent" in p for p in guest_put.compare(h, extra)))
    check("no answer from the VM is a failure", guest_put.compare(h, None) != [])
    for p in ("~/In box", "/tmp/x", "~"):
        guest_put.guest_dir(p, "cua")
    try:
        guest_put.guest_dir("~/$(reboot)", "cua")
        ok = False
    except guest_put.PutError:
        ok = True
    check("put refuses shell characters in the guest folder", ok)


# --- fixes from the storage audit (local/audits/storage-2026-11-18) ---------------------------------
def test_case_aliases():
    """Audit F3: another spelling of a refused folder (case, the data volume's firmlinks) is the same folder."""
    home = os.path.expanduser("~")
    aliases = {"~/library (case)": home + "/library",
               "/USERS/<you>/LIBRARY (case)": "/USERS" + home[len("/Users"):] + "/LIBRARY",
               "/system/volumes/data/... /Documents (firmlink)": "/system/volumes/data" + home + "/Documents",
               "~/DOCUMENTS (case)": home + "/DOCUMENTS"}
    if os.path.isdir(home + "/.ssh"):
        aliases["~/.SSH (case)"] = home + "/.SSH"
    for what, p in aliases.items():
        if os.path.isdir(p):
            check(f"refused under another spelling: {what}", raises(storage.check_shareable, p) is not None)
    fh = os.path.join(TMP, "fs", "Users", "u")
    where = dict(home=fh, lab=os.path.join(fh, "lab"), ssh_dir=os.path.join(fh, "keys"),
                 data=os.path.join(fh, ".local", "share", "metacomputer"), state=os.path.join(TMP, "state"))
    if os.path.isdir(os.path.join(fh, "LIBRARY")):          # a case-insensitive volume
        check("refused (made-up home): LIBRARY", raises(storage.check_shareable, os.path.join(fh, "LIBRARY"), **where))
        check("refused (made-up home): PROJECT/SNEAKY (a link to .ssh, spelled in capitals)",
              raises(storage.check_shareable, os.path.join(fh, "PROJECT", "SNEAKY"), **where))
        real = storage.check_shareable(os.path.join(fh, "PROJECT"), **where)
        check("an allowed folder comes back in its real spelling", real.endswith("/project"), real)
        a = os.path.join(TMP, "lockcase.img")
        open(a, "w").close()
        it1 = {"kind": "disk", "name": "a", "path": a, "lock": storage.lock_key("disk", a)}
        it2 = {"kind": "disk", "name": "b", "path": a.upper(), "lock": storage.lock_key("disk", a.upper())}
        storage.acquire([it1], "vm-a", "/lab", os.getpid())
        e = raises(storage.acquire, [it2], "vm-b", "/lab", os.getpid())
        storage.release_locks("vm-a", "/lab")
        check("one image under two spellings is one lock", e is not None, e)


def test_refdisk_swap():
    """Audit F4: a file swapped for a link to a private file while the folder is being packed isn't packed."""
    src = os.path.join(TMP, "swapsrc")
    os.makedirs(src)
    secret = os.path.join(TMP, "private-secret.txt")
    open(secret, "w").write("SECRET-DO-NOT-PACK\n")
    open(os.path.join(src, "victim.txt"), "w").write("harmless\n")
    open(os.path.join(src, "other.txt"), "w").write("other\n")
    dst = os.path.join(TMP, "swapdst")
    os.makedirs(dst)
    real_lstat, real_stat = os.lstat, os.stat
    done = {"x": False}

    def swap_after(fn):
        def inner(path, *a, **k):
            r = fn(path, *a, **k)
            name = path if isinstance(path, str) else ""
            if not done["x"] and os.path.basename(name) == "victim.txt":
                done["x"] = True
                os.unlink(os.path.join(src, "victim.txt"))
                os.symlink(secret, os.path.join(src, "victim.txt"))
            return r
        return inner
    os.lstat, os.stat = swap_after(real_lstat), swap_after(real_stat)
    try:
        try:
            storage.stage_tree(src, dst)
        except Exception as e:  # noqa: BLE001
            print(f"  (stage_tree raised {e!r})")
    finally:
        os.lstat, os.stat = real_lstat, real_stat
    leaked = []
    for d, _, fs in os.walk(dst):
        for f in fs:
            if "SECRET-DO-NOT-PACK" in open(os.path.join(d, f), errors="replace").read():
                leaked.append(f)
    check("a file swapped for a link to a private file mid-pack isn't packed (the swap happened)",
          done["x"] and not leaked, leaked)


def test_bounded_replies():
    """Audit F6: what the VM sends back is read with a size cap and a deadline, not collected first."""
    big = [sys.executable, "-c", "import sys; sys.stdout.write('x' * (80 << 20))"]
    t0 = time.time()
    try:
        guest_put.remote(big, "true", timeout=30)
        ok = False
    except guest_put.PutError:
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = f"raised {e!r}"
    check("put: a VM that floods its answer is cut off (not read in full)", ok is True, ok)
    hang = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.time()
    try:
        guest_put.remote(hang, "true", timeout=2)
        ok = False
    except guest_put.PutError:
        ok = time.time() - t0 < 10
    except Exception as e:  # noqa: BLE001
        ok = f"raised {e!r}"
    check("put: a VM that never answers is given up on at the deadline", ok is True, ok)
    if not hasattr(storage, "guest_call"):
        check("storage attach reads the VM's answer bounded (guest_call)", False, "no guest_call")
        return
    fake = os.path.join(TMP, "fake-admin-ssh")
    for mode, body in (("flood", "import sys; sys.stdout.write('{' * (8 << 20))"),
                       ("hang", "import time; time.sleep(60)"),
                       ("ok", "import sys; print('1') if sys.argv[-1].endswith('version') else print('{\"ok\": true, \"items\": []}')")):
        open(fake, "w").write(f"#!{sys.executable}\n{body}\n")
        os.chmod(fake, 0o755)
        t0 = time.time()
        rc, out, why = storage.guest_call([fake], "version" if mode == "ok" else "attach", b"{}", timeout=3)
        if mode == "ok":
            check("storage attach: a normal answer comes through", rc == 0 and out.strip() == "1", (rc, out, why))
        else:
            check(f"storage attach: a VM that {'floods' if mode == 'flood' else 'stalls'} is refused, quickly",
                  rc != 0 and why and time.time() - t0 < 15, (rc, len(out), why))


def test_reservations():
    """Audit F12: checkpoint, restore, resize and delete hold the volume while they work."""
    os.environ["MCOMP_VOLUMES"] = "rv"
    rec = make_volume("rv")
    img = rec["path"]
    other = {"kind": "disk", "name": "rv", "path": img, "lock": storage.lock_key("disk", img)}
    seen = {}
    real = storage.clone_or_copy

    def probe(src, dst):
        e = raises(storage.acquire, [other], "intruder-vm", "/elsewhere", os.getpid())
        seen.setdefault("during", []).append(e)
        if e is None:
            storage.release_locks("intruder-vm", "/elsewhere")
        return real(src, dst)
    storage.clone_or_copy = probe
    try:
        quiet(storage.cmd_checkpoint, Args(op="save", name="rc1"))
        quiet(storage.cmd_checkpoint, Args(op="restore", name="rc1"))
    finally:
        storage.clone_or_copy = real
    check("while a checkpoint is saved or restored, no VM can attach the volume",
          len(seen.get("during", [])) == 2 and all(seen["during"]), seen)
    real_trunc = storage.os.ftruncate
    during = []

    def trunc(fd, n):
        during.append(raises(storage.acquire, [other], "intruder-vm", "/elsewhere", os.getpid()))
        storage.release_locks("intruder-vm", "/elsewhere")
        return real_trunc(fd, n)
    storage.os.ftruncate = trunc
    try:
        quiet(storage.cmd_volume_resize, Args(name="rv", size="32M"))
    finally:
        storage.os.ftruncate = real_trunc
    check("...nor while it's resized", during and all(during), during)
    check("...and the volume is free again afterwards", storage.holder_of(img, "disk") is None
          and raises(storage.acquire, [other], "next-vm", "/lab", os.getpid()) is None)
    storage.release_locks("next-vm", "/lab")
    storage.cmd_checkpoint(Args(op="delete", name="rc1"))
    os.environ["MCOMP_VOLUMES"] = ""


def test_manifest_parsing():
    """Audit F13: the bounded manifest is read as YAML, not with patterns."""
    tricky = ("version: 3\nallow:\n  tools: [list_windows]\nresources:\n  files:\n    write:\n"
              "      - {dir: /data, recursive: false} # previously recursive: true\n  desktop:\n    display: true\n")
    read, write = storage.manifest_dirs(tricky)
    check("an inline comment isn't policy: recursive: false stays false", write == [("/data", False)], (read, write))
    elsewhere = ("version: 3\nnotes:\n  files:\n    write:\n      - {dir: /data, recursive: true}\nresources:\n"
                 "  desktop:\n    display: true\n")
    read, write = storage.manifest_dirs(elsewhere)
    check("a files: block outside resources grants nothing", read == [] and write == [], (read, write))
    for bad in ("resources:\n  files:\n    write:\n      - {dir: /data, recursive: yes}\n",
                "resources:\n  files:\n    write:\n      - &a {dir: /data, recursive: true}\n",
                "resources:\n  files:\n    write:\n      - {dir: /data, recursive: true}\n      - {dir: /data, recursive: true, dir: /x}\n"):
        try:
            r = storage.manifest_dirs(bad)
            ok = r == ([], [])
        except Exception:  # noqa: BLE001
            ok = True
        check(f"an unsupported or ambiguous manifest grants nothing: {bad.splitlines()[-1].strip()[:50]}", ok)
    import driver_profiles
    for prof in ("readonly", "desktop", "browser"):
        os.environ.update(MCOMP_DRIVER_PROFILE=prof, MCOMP_DRIVER_ORIGINS="https://example.com", MCOMP_BROWSERS="firefox chrome")
        read, write = storage.manifest_dirs(driver_profiles.render())
        want = (prof == "readonly" and read == [] and write == []) or \
            (prof != "readonly" and (H + "/metacomputer-runs", True) in read and (H + "/metacomputer-runs", True) in write)
        check(f"the shipped '{prof}' profile parses exactly (its task folders, or none)", want, (read, write))
    os.environ.update(MCOMP_DRIVER_PROFILE="desktop", MCOMP_DRIVER_ORIGINS="")


def test_folders():
    os.environ.update(MCOMP_VOLUMES="", MCOMP_REFDISKS="")
    base = os.path.join(TMP, "folders")
    plain = os.path.join(base, "plain")
    noidx = os.path.join(base, "share.noindex")
    inner = os.path.join(noidx, "inner")
    for d in (plain, inner, os.path.join(base, "has:colon")):
        os.makedirs(d, exist_ok=True)
    quiet(storage.cmd_volume_create, Args(name="fplain", folder=plain, size=None, dir=None))
    quiet(storage.cmd_volume_create, Args(name="fnoidx", folder=noidx, size=None, dir=None))
    quiet(storage.cmd_volume_create, Args(name="finner", folder=inner, size=None, dir=None))
    check("a folder volume records the real folder", storage.reg_load("volume", "fplain")["path"] == os.path.realpath(plain))
    check("a folder in a refused place is refused", raises(storage.cmd_volume_create, Args(
        name="fbad", folder=os.path.expanduser("~/Library"), size=None, dir=None)) is not None)
    check("a folder whose path has ':' is refused (the VM runner splits on it)", raises(storage.cmd_volume_create, Args(
        name="fcolon", folder=os.path.join(base, "has:colon"), size=None, dir=None)) is not None)
    check("--size makes no sense for a folder", raises(storage.cmd_volume_create, Args(
        name="fx", folder=plain, size="1G", dir=None)) is not None)
    os.environ["MCOMP_VOLUMES"] = "fplain, fnoidx:/data/shared:rw"
    items, problems, notes = storage.build_plan()
    check("folders plan read-only by default, rw when asked", not problems and [i["rw"] for i in items] == [False, True],
          (problems, items))
    check("...the read-write one takes an exclusive record, the read-only one a shared one",
          items[1]["exclusive"] is True and items[0]["exclusive"] is False and items[0]["lock"] and items[1]["lock"])
    args = storage.lume_args(items)
    check("run arguments: --shared-dir PATH:ro and PATH:rw", args == ["--shared-dir", os.path.realpath(plain) + ":ro",
                                                                      "--shared-dir", os.path.realpath(noidx) + ":rw"], args)
    plan = storage.guest_plan(items)
    check("the VM's plan names each folder's share", [f["share"] for f in plan["folders"]] == ["plain", "share.noindex"]
          and plan["folders"][1]["rw"] is True, plan["folders"])
    h = helper()
    try:
        h.check_plan(plan, H)
        ok = True
    except h.Refused as e:
        ok = str(e)
    check("...and the VM's helper accepts it", ok is True, ok)
    check("share names follow the VM runner: the last component, ' (2)' for a repeat",
          storage.share_names(["/a/data", "/b/data", "/c/x", "/d/data"]) == ["data", "data (2)", "x", "data (3)"])
    os.environ["MCOMP_VOLUMES"] = "fplain:rw"
    e = storage.build_plan()[1]
    check("read-write without .noindex is refused, saying how to fix it", any(".noindex" in p and "Spotlight" in p
                                                                             for p in e), e)
    os.environ["MCOMP_VOLUMES"] = "fnoidx:rw"
    items = storage.build_plan()[0]
    storage.acquire(items, "vm-a", "/lab", os.getpid())
    e = raises(storage.acquire, items, "vm-b", "/lab", os.getpid())
    check("a folder is read-write in one VM at a time", e and "vm-a" in e, e)
    os.environ["MCOMP_VOLUMES"] = "finner:rw"
    e = raises(storage.acquire, storage.build_plan()[0], "vm-b", "/lab", os.getpid())
    check("...and a folder inside it can't be read-write elsewhere either", e and "vm-a" in e, e)
    os.environ["MCOMP_VOLUMES"] = "finner"
    e = raises(storage.acquire, storage.build_plan()[0], "vm-b", "/lab", os.getpid())
    check("...nor read-only (vm-a could swap it for another folder)", e and "inside a folder shared read-write" in e, e)
    os.environ["MCOMP_VOLUMES"] = "fnoidx"
    check("...while the same folder read-only elsewhere is fine (vm-a can't replace the folder it was given)",
          raises(storage.acquire, storage.build_plan()[0], "vm-b", "/lab", os.getpid()) is None)
    storage.release_locks("vm-b", "/lab")
    storage.release_locks("vm-a", "/lab")
    os.environ["MCOMP_VOLUMES"] = ""


def gatekeeper(path):
    """Run a binary: 'ran' when it exits 0 or 3 (the test programs' codes), else 'refused' (killed, refused,
    or held by Gatekeeper past 20 s)."""
    try:
        r = subprocess.run([path], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "refused"
    return "ran" if r.returncode in (0, 3) else "refused"


def test_watcher():
    root = os.path.join(TMP, "watched.noindex")
    os.makedirs(os.path.join(root, "sub"))
    open(os.path.join(root, "before.txt"), "w").write("the Mac's own file")
    os.symlink("/etc", os.path.join(root, "mac-link"))          # the Mac's own link, there before the VM
    snap = storage.walk_sigs(root)
    ours, stats = set(), {"quarantined": 0, "restored": 0, "links_removed": 0, "special_removed": 0, "errors": 0}
    # what a VM would do through the share
    open(os.path.join(root, "new.txt"), "w").write("from the VM")
    os.makedirs(os.path.join(root, "made.app", "Contents"))
    os.symlink(os.path.expanduser("~/.ssh"), os.path.join(root, "escape-abs"))
    os.symlink("../../../..", os.path.join(root, "sub", "escape-rel"))
    os.symlink("new.txt", os.path.join(root, "inside-link"))
    os.mkfifo(os.path.join(root, "pipe"))
    tool = os.path.join(root, "tool")
    # An unsigned (ad-hoc signed) program, like one built in the VM; Apple's own binaries always run.
    src = os.path.join(TMP, "tool.c")
    open(src, "w").write("int main(void){return 0;}\n")
    # Running a quarantined binary can make Gatekeeper hold it for minutes or show a dialog on the Mac's screen,
    # so these checks run only with MCOMP_TEST_GATEKEEPER=1 (docs/storage.md has the results on macOS 27).
    gk = os.environ.get("MCOMP_TEST_GATEKEEPER") == "1"
    built = gk and shutil.which("cc") and subprocess.run(["cc", "-o", tool, src], capture_output=True).returncode == 0
    if not built:
        shutil.copy("/usr/bin/true", tool)
    os.chmod(tool, 0o4755)
    tool2 = os.path.join(root, "tool2")
    open(src, "w").write("int main(void){return 3;}\n")
    if not built or subprocess.run(["cc", "-o", tool2, src], capture_output=True).returncode != 0:
        shutil.copy("/usr/bin/true", tool2)
    cur, links, special = storage.protect(root, snap, ours, stats)
    q = lambda p: storage.get_xattr(os.path.join(root, p), storage.QUARANTINE)  # noqa: E731
    check("a file the VM wrote gets the quarantine attribute", q("new.txt") and storage.QUAR_OK.match(q("new.txt")),
          q("new.txt"))
    check("...and so does a folder (an app bundle is one)", q("made.app") is not None)
    check("the Mac's own file from before the VM booted is left alone", q("before.txt") is None)
    check("a link out of the folder (absolute) is replaced by a note",
          not os.path.lexists(os.path.join(root, "escape-abs")) and os.path.isfile(os.path.join(root, "escape-abs.link-removed.txt")))
    check("...and so is one that climbs out with ..", not os.path.lexists(os.path.join(root, "sub", "escape-rel")))
    check("a link that stays inside the folder is kept", os.path.islink(os.path.join(root, "inside-link")))
    check("the Mac's own link from before is left alone", os.path.islink(os.path.join(root, "mac-link")))
    check("a FIFO is removed", not os.path.lexists(os.path.join(root, "pipe")) and special == ["pipe"], special)
    check("set-id bits are removed", not os.stat(tool).st_mode & 0o6000, oct(os.stat(tool).st_mode))
    check("it reports what it did", stats["links_removed"] == 2 and stats["special_removed"] == 1 and
          stats["quarantined"] >= 4 and len(links) == 2, stats)
    if built:
        check("Gatekeeper refuses to run an unsigned binary the VM wrote (it carries our quarantine)",
              gatekeeper(tool) == "refused")
    else:
        print("SKIP running quarantined binaries (MCOMP_TEST_GATEKEEPER=1 runs them; needs a C compiler)")
    subprocess.run(["xattr", "-d", storage.QUARANTINE, os.path.join(root, "new.txt")], check=True)
    subprocess.run(["xattr", "-w", storage.QUARANTINE, "00c1;6745a1b2;Safari;", tool], check=True)
    subprocess.run(["xattr", "-w", storage.QUARANTINE, "00c1;6745a1b2;Safari;", tool2], check=True)
    if built:
        check("(a rewritten value with the 'user approved' flag would let it run: why it's enforced)",
              gatekeeper(tool) == "ran")
    cur, links, special = storage.protect(root, cur, ours, stats)
    check("a removed attribute is put back, and a rewritten one replaced", storage.QUAR_OK.match(q("new.txt") or b"")
          and storage.QUAR_OK.match(q("tool2") or b"") and stats["restored"] == 3, (q("new.txt"), q("tool2"), stats))
    # (tool ran while its value said "approved", and macOS remembers that: putting the attribute back doesn't
    # stop that same binary again. tool2 never ran, so the restored attribute holds.)
    if built:
        check("...so Gatekeeper refuses a binary whose attribute the VM rewrote, once it's put back",
              gatekeeper(tool2) == "refused")
    cur2, links, special = storage.protect(root, cur, ours, stats)
    check("a pass with nothing new changes nothing", stats["restored"] == 3 and not links and not special)


def protect_once(root, prev=None, ours=None, stats=None, **kw):
    """One watcher pass over root, whatever the watcher's internals: returns (picture, stats)."""
    stats = stats if stats is not None else {"quarantined": 0, "restored": 0, "links_removed": 0, "special_removed": 0,
                                             "errors": 0}
    ours = ours if ours is not None else set()
    if hasattr(storage, "protect_path"):
        cur = storage.protect_path(root, prev or {}, ours, stats, **kw)[0]
    else:
        cur = storage.protect(root, prev or {}, ours, stats)[0]
    return cur, stats


def test_watcher_ancestor_swap():
    """Audit F2: a folder swapped for a link to a private folder mid-pass: nothing outside is touched."""
    root = os.path.join(TMP, "swap.noindex")
    private = os.path.join(TMP, "private-dir")
    os.makedirs(os.path.join(root, "d"))
    os.makedirs(private)
    open(os.path.join(private, "config"), "w").write("the Mac's ssh config\n")
    open(os.path.join(private, "new.txt"), "w").write("private\n")
    os.mkfifo(os.path.join(root, "d", "config"))
    open(os.path.join(root, "d", "new.txt"), "w").write("from the VM\n")
    real_scandir = os.scandir
    done = {"x": False}

    def scandir(arg=None):
        it = real_scandir(arg) if arg is not None else real_scandir()
        entries = list(it)
        it.close()
        if not done["x"] and any(e.name == "config" for e in entries):
            done["x"] = True
            os.rename(os.path.join(root, "d"), os.path.join(root, "d-was"))
            os.symlink(private, os.path.join(root, "d"))

        class It(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                pass
        return It(entries)
    os.scandir = scandir
    try:
        try:
            protect_once(root)
        except Exception as e:  # noqa: BLE001
            print(f"  (the pass raised {e!r})")
    finally:
        os.scandir = real_scandir
    check("a folder swapped for a link mid-pass: the private file the FIFO's path now names isn't deleted",
          done["x"] and os.path.isfile(os.path.join(private, "config")))
    check("...and the private folder's files get no attribute through it",
          storage.get_xattr(os.path.join(private, "new.txt"), storage.QUARANTINE) is None)


def test_watcher_limits():
    """Audit F7: a folder the watcher can't cover completely is a protection failure, not silence."""
    root = os.path.join(TMP, "big.noindex")
    os.makedirs(root)
    for i in range(30):
        open(os.path.join(root, f"f{i}"), "w").close()
    real = storage.MAX_WATCH
    storage.MAX_WATCH = 10
    try:
        try:
            cur, st = protect_once(root)
            ok = False
        except getattr(storage, "ProtectionFailure", ()) or Exception:
            ok = hasattr(storage, "ProtectionFailure")
    finally:
        storage.MAX_WATCH = real
    check("past the watcher's limit, the pass fails loudly (ProtectionFailure)", ok)
    root2 = os.path.join(TMP, "locked.noindex")
    os.makedirs(os.path.join(root2, "hidden"))
    os.mkfifo(os.path.join(root2, "hidden", "pipe"))
    os.chmod(os.path.join(root2, "hidden"), 0)
    try:
        protect_once(root2)
    except Exception as e:  # noqa: BLE001
        print(f"  (the pass raised {e!r})")
    os.chmod(os.path.join(root2, "hidden"), 0o755)
    check("a folder the VM made unreadable is opened again and checked (its FIFO goes)",
          not os.path.lexists(os.path.join(root2, "hidden", "pipe")))


def test_quarantine_race():
    """Audit F8: the attribute removed right after the watcher set it is put back on a later pass."""
    root = os.path.join(TMP, "race.noindex")
    os.makedirs(root)
    f = os.path.join(root, "vm.txt")
    open(f, "w").write("x")
    names = [n for n in ("fsetxattr_fd", "set_xattr") if hasattr(storage, n)]
    real = {n: getattr(storage, n) for n in names}

    def racing(name):
        def inner(*a, **k):
            r = real[name](*a, **k)
            subprocess.run(["xattr", "-d", storage.QUARANTINE, f], capture_output=True)   # the guest, right after
            return r
        return inner
    for n in names:
        setattr(storage, n, racing(n))
    try:
        cur, st = protect_once(root)
    finally:
        for n in names:
            setattr(storage, n, real[n])
    cur, st = protect_once(root, cur, stats=st, ours={"vm.txt"} | set())
    v = storage.get_xattr(f, storage.QUARANTINE)
    check("a quarantine the guest removed right after it was set is back after the next pass",
          v is not None and storage.QUAR_OK.match(v), v)


def test_low_space():
    """Audit F10 (folders): a read-write folder filling the Mac's disk stops protection, loudly."""
    root = os.path.join(TMP, "space.noindex")
    os.makedirs(root)
    if not hasattr(storage, "free_bytes_at"):
        check("the watcher checks the Mac's free space (free_bytes_at)", False)
        return
    real = storage.free_bytes_at
    storage.free_bytes_at = lambda fd: 1 << 20
    try:
        try:
            protect_once(root)
            ok = False
        except storage.ProtectionFailure as e:
            ok = "free" in str(e)
    finally:
        storage.free_bytes_at = real
    check("with less free space than IMPORT_MIN_FREE_GB, the pass fails (the VM is stopped)", ok)


def test_folder_binding():
    """Audit F5: what start attaches is what it checked, and no VM's read-write share can swap it."""
    base = os.path.join(TMP, "bind")
    parent = os.path.join(base, "parent.noindex")
    child = os.path.join(parent, "child")
    os.makedirs(child)
    for n, path in (("bparent", parent), ("bchild", child)):
        quiet(storage.cmd_volume_create, Args(name=n, folder=path, size=None, dir=None))
    os.environ["MCOMP_VOLUMES"] = "bparent:rw"
    storage.acquire(storage.build_plan()[0], "vm-a", "/lab", os.getpid())
    os.environ["MCOMP_VOLUMES"] = "bchild"
    items = storage.build_plan()[0]
    e = raises(storage.acquire, items, "vm-b", "/lab", os.getpid())
    check("a folder inside another VM's read-write share is refused, even read-only", e is not None, e)
    storage.release_locks("vm-a", "/lab")
    os.environ["MCOMP_VOLUMES"] = "bchild"
    storage.acquire(storage.build_plan()[0], "vm-b", "/lab", os.getpid())
    os.environ["MCOMP_VOLUMES"] = "bparent:rw"
    e = raises(storage.acquire, storage.build_plan()[0], "vm-a", "/lab", os.getpid())
    check("a read-write share holding another VM's folder is refused", e is not None, e)
    storage.release_locks("vm-b", "/lab")
    img = os.path.join(parent, "inner.img")
    with open(img, "wb") as f:
        f.truncate(16 << 20)
    storage.reg_save("volume", {"name": "binner", "kind": "disk", "path": img, "uuid": "11111111-2222-4333-8444-555555555555",
                                "created": "x"})
    os.environ["MCOMP_VOLUMES"] = "bparent:rw"
    storage.acquire(storage.build_plan()[0], "vm-a", "/lab", os.getpid())
    os.environ["MCOMP_VOLUMES"] = "binner"
    e = raises(storage.acquire, storage.build_plan()[0], "vm-c", "/lab", os.getpid())
    check("a disk image inside another VM's read-write share is refused", e is not None, e)
    storage.release_locks("vm-a", "/lab")
    os.environ["MCOMP_VOLUMES"] = "bchild"
    items = storage.build_plan()[0]
    os.rename(child, child + "-was")
    os.makedirs(child)
    changed = storage.changed_items(items)
    check("a folder replaced after it was checked is noticed (start stops the VM)", changed, changed)
    os.environ["MCOMP_VOLUMES"] = ""


def test_watcher_started_first():
    """Audit F6 (folders): the watcher starts with the VM, before the VM is asked anything, or the VM stops."""
    root = os.path.join(TMP, "first.noindex")
    os.makedirs(root)
    quiet(storage.cmd_volume_create, Args(name="wfirst", folder=root, size=None, dir=None))
    os.environ["MCOMP_VOLUMES"] = "wfirst:rw"
    with open(os.path.join(TMP, "ssh", "guest-features"), "w") as f:
        f.write("storage=1\n")
    vm = subprocess.Popen(["sleep", "60"])
    open(os.path.join(TMP, "run", "lume.pid"), "w").write(str(vm.pid))
    try:
        quiet(storage.cmd_prepare, Args(pid=os.getpid(), no_volumes=False))
        real = storage.watcher_start
        storage.watcher_start = lambda: 0
        try:
            rc = quiet(storage.cmd_started, Args(pid=vm.pid))
        finally:
            storage.watcher_start = real
        check("if the watcher can't start, start fails (and stops the VM)", rc != 0, rc)
        rc = quiet(storage.cmd_started, Args(pid=vm.pid))
        pid = storage.watcher_alive()
        check("the watcher starts when the VM's process does, before the VM is asked to mount anything",
              rc == 0 and pid, (rc, pid))
        open(os.path.join(root, "late"), "w").close()
        os.mkfifo(os.path.join(root, "pipe"))
        time.sleep(5)
        check("...and it works from then on (a FIFO goes)", not os.path.lexists(os.path.join(root, "pipe")))
        storage.MAX_WATCH_SAVED = storage.MAX_WATCH
        for i in range(12):
            os.mkfifo(os.path.join(root, f"p{i}"))
        stats = storage.watcher_stop()
        check("the watcher stops when asked", not storage.watcher_alive())
    finally:
        vm.kill()
        vm.wait()
        storage.release_locks(storage.vm_name(), storage.lab_root())
        storage.clear_state()
        os.environ["MCOMP_VOLUMES"] = ""


def test_watcher_failure_stops_vm():
    """Audit F6/F7: when protection can't run (here: over the watcher's limit), the watcher stops the VM."""
    if not hasattr(storage, "ProtectionFailure"):
        check("a protection failure stops the VM", False, "no ProtectionFailure")
        return
    root = os.path.join(TMP, "fail.noindex")
    os.makedirs(root)
    quiet(storage.cmd_volume_create, Args(name="wfail", folder=root, size=None, dir=None))
    os.environ["MCOMP_VOLUMES"] = "wfail:rw"
    vm = subprocess.Popen(["sleep", "60"])
    open(os.path.join(TMP, "run", "lume.pid"), "w").write(str(vm.pid))
    try:
        quiet(storage.cmd_prepare, Args(pid=os.getpid(), no_volumes=False))
        for i in range(40):
            open(os.path.join(root, f"f{i}"), "w").close()
        env = dict(os.environ, MCOMP_WATCH_MAX="20")
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "storage.py"), "watch"], env=env,
                           capture_output=True, text=True, timeout=60)
        try:
            vm.wait(10)
            stopped = True
        except subprocess.TimeoutExpired:
            stopped = False
        check("a protection failure stops the VM (its process is ended) and says why", stopped and r.returncode != 0
              and "protection" in (r.stdout + r.stderr).lower(), (stopped, r.returncode, r.stderr[-300:]))
    finally:
        if vm.poll() is None:
            vm.kill()
        vm.wait()
        storage.release_locks(storage.vm_name(), storage.lab_root())
        storage.clear_state()
        os.environ["MCOMP_VOLUMES"] = ""


def main():
    try:
        test_parsing()
        test_refusals()
        test_locks()
        test_network()
        test_plan()
        test_sizes_names()
        test_copies_and_checkpoints()
        test_put()
        test_case_aliases()
        test_refdisk_swap()
        test_bounded_replies()
        test_reservations()
        test_manifest_parsing()
        test_folders()
        test_watcher()
        test_watcher_ancestor_swap()
        test_watcher_limits()
        test_quarantine_race()
        test_low_space()
        test_folder_binding()
        test_watcher_started_first()
        test_watcher_failure_stops_vm()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
