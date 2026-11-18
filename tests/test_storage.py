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
- put: SHA-256 comparison (missing, changed, extra files) and the "name (1).ext" collision names.
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
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
