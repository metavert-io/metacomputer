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
- network filesystems: the statfs classifier, a local folder is local, a network one warns; and a
  real one (WebDAV served by the pinned rclone on 127.0.0.1, mounted with macOS's mount_webdav):
  detected, warned about at create and start, refused once unmounted;
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
  what was there before the VM booted is left alone;
- S3 volumes: names, endpoints, prefixes and regions checked; the keys never reach argv (of
  `security` or rclone), the registry, rclone.conf, logs or events, only rclone's environment
  (stand-ins for `security` and rclone record what they were given); `volume keys` refuses
  without a terminal and takes no value; a sync's conflicts are reported and both copies
  kept; the pinned rclone: a changed binary or wrong version is refused, and
  scripts/fetch-artifacts.sh rclone, against a local server and a throwaway signing key,
  installs a correctly signed and pinned release but refuses a wrong pinned digest, a
  SHA256SUMS signed by another key, and a tampered zip.
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


def test_webdav_standin():
    """A real network filesystem on this Mac, with nothing outside it: the pinned rclone serves a folder over
    WebDAV on 127.0.0.1 and macOS's own mount_webdav mounts it (no root needed)."""
    ver = storage.env("RCLONE_VERSION", "")
    rclone = os.path.join(LAB, ".cache", f"rclone-v{ver}", "rclone")
    if not (os.path.isfile(rclone) and os.path.exists("/sbin/mount_webdav")):
        print("SKIP the WebDAV stand-in (needs the pinned rclone: scripts/fetch-artifacts.sh rclone)")
        return
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    d = os.path.join(TMP, "netfs")
    os.makedirs(os.path.join(d, "served"))
    os.makedirs(os.path.join(d, "mnt"))
    srv = subprocess.Popen([rclone, "serve", "webdav", "--addr", f"127.0.0.1:{port}", "--config", "/dev/null",
                            os.path.join(d, "served")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")})
    mnt = os.path.join(d, "mnt")
    try:
        import time
        time.sleep(1.5)
        r = subprocess.run(["/sbin/mount_webdav", "-S", f"http://127.0.0.1:{port}/", mnt], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"SKIP the WebDAV stand-in (mount_webdav: {r.stderr.strip()[:100]})")
            return
        t, where, local = storage.fs_info(mnt)
        check("a WebDAV mount (a real network filesystem) is detected by statfs as not local",
              t == "webdav" and not local and storage.network_fs(mnt) is not None, (t, where, local))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            storage.cmd_volume_create(Args(name="netdav", size="16M", dir=mnt))
        check("...a disk volume created on it is warned about (drops, locks across computers)",
              "network filesystem (webdav" in out.getvalue(), out.getvalue())
        os.environ["MCOMP_VOLUMES"] = "netdav"
        items, problems, notes = storage.build_plan()
        check("...and so is every start that attaches it", items and any("network filesystem" in n for n in notes), notes)
        subprocess.run(["/sbin/umount", mnt], capture_output=True, timeout=30)
        check("with the share gone, the start is refused (is its drive connected?)",
              any("missing" in p and "connected" in p for p in storage.build_plan()[1]))
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
        subprocess.run(["/sbin/umount", mnt], capture_output=True, timeout=30)
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        storage.reg_delete("volume", "netdav")


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

    def probe(src, dst, **kw):
        e = raises(storage.acquire, [other], "intruder-vm", "/elsewhere", os.getpid())
        seen.setdefault("during", []).append(e)
        if e is None:
            storage.release_locks("intruder-vm", "/elsewhere")
        return real(src, dst, **kw)
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
    items, problems, _ = storage.build_plan()
    check("a folder holding a disk image can't be shared at all (whatever holds what, and when)",
          not items and problems, (items, problems))
    storage.reg_delete("volume", "binner")
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

FAKE_SECURITY = """#!/bin/sh
printf '%s\\n' "$*" >> "$MCST_SEC_LOG"
case "$*" in *find-generic-password*access_key_id*-w*) echo "AKIDTESTVALUE0123456789" ;;
             *find-generic-password*secret_access_key*-w*) echo "SECRETTESTVALUE/abc+xyz0123456789" ;;
             *find-generic-password*) echo 'keychain: "x"' ;; *) ;; esac
"""
FAKE_RCLONE = """#!/bin/sh
printf '%s\\n' "$*" >> "$MCST_RC_ARGS"
env | sort >> "$MCST_RC_ENV"
case "$*" in *version*) echo "rclone v1.75.2" ;;
  *bisync*) echo "2026/01/01 NOTICE: - WARNING  New or changed in both paths       - notes.txt"
            echo "2026/01/01 NOTICE: - Path1    Renaming Path1 copy      - /x/notes.conflict1.txt"
            echo "2026/01/01 NOTICE: - WARNING  New or changed in both paths       - same.txt" ;; esac
exit 0
"""
KEY_ID, KEY_SECRET = "AKIDTESTVALUE0123456789", "SECRETTESTVALUE/abc+xyz0123456789"
FAKE_SEC_PATH = [None]


def test_s3():
    d = os.path.join(TMP, "s3fake")
    os.makedirs(d)
    sec, rcl = os.path.join(d, "security"), os.path.join(d, "rclone")
    FAKE_SEC_PATH[0] = sec
    logs = {"MCST_SEC_LOG": os.path.join(d, "sec.log"), "MCST_RC_ARGS": os.path.join(d, "rc-args.log"),
            "MCST_RC_ENV": os.path.join(d, "rc-env.log")}
    for path, text in ((sec, FAKE_SECURITY), (rcl, FAKE_RCLONE)):
        for k, v in logs.items():           # rclone runs with a clean environment: the paths go in the script
            text = text.replace(f'"${k}"', f'"{v}"')
        open(path, "w").write(text)
        os.chmod(path, 0o755)
    bad = {"a bucket name with capitals": dict(bucket="MyBucket"), "a bucket name too short": dict(bucket="ab"),
           "an endpoint that isn't a URL": dict(endpoint="s3.amazonaws.com"),
           "an endpoint with a path or query": dict(endpoint="https://x.example/?a=b"),
           "a prefix with ..": dict(prefix="a/../b"), "a bad region": dict(region="US EAST")}
    for what, over in bad.items():
        a = dict(name="s3x", s3=True, bucket="my-bucket", endpoint="https://s3.example.com", region=None, prefix=None,
                 dir=None, size=None, folder=None)
        a.update(over)
        check(f"S3 volume refused: {what}", raises(storage.cmd_volume_create, Args(**a)) is not None)
    quiet(storage.cmd_volume_create, Args(name="s3v", s3=True, bucket="my-bucket", endpoint="http://127.0.0.1:9000/",
                                          region="us-east-1", prefix="/team/work/", dir=None, size=None, folder=None))
    rec = storage.reg_load("volume", "s3v")
    check("an S3 volume: a local copy named .noindex in STORAGE_DIR, the bucket recorded, no keys in the registry",
          rec["path"].endswith("/volumes/s3v.noindex") and os.path.isdir(rec["path"]) and rec["s3"]["prefix"] == "team/work"
          and rec["s3"]["endpoint"] == "http://127.0.0.1:9000" and "key" not in json.dumps(rec).lower(), rec)
    os.environ["MCOMP_VOLUMES"] = "s3v:rw"
    items = storage.build_plan()[0]
    check("it attaches as a folder volume (its local copy), read-write when asked",
          items and items[0]["kind"] == "s3" and storage.lume_args(items) == ["--shared-dir", rec["path"] + ":rw"], items)
    os.environ["MCOMP_VOLUMES"] = ""
    open(os.path.join(rec["path"], "notes.txt"), "w").write("x")
    # unconfined here: this stand-in logs to a temporary folder, which the sandbox (checked in test_s3_sandbox) denies
    r = storage.s3_sync(rec, reason="manual", rclone=rcl, security=sec, resync=True, sandbox=False)
    check("a sync runs rclone bisync and reports the conflict it kept twice (not one that ended up identical)",
          r["ok"] and r["conflicts"] == ["notes.txt"], r)
    args = open(logs["MCST_RC_ARGS"]).read()
    renv = open(logs["MCST_RC_ENV"]).read()
    seclog = open(logs["MCST_SEC_LOG"]).read()
    check("bisync keeps both copies on a conflict (newer wins the name; the other gets a .conflictN name)",
          "--conflict-resolve newer --conflict-loser num --conflict-suffix conflict" in args and "--resync" in args, args)
    check("the keys are not in rclone's arguments", KEY_ID not in args and KEY_SECRET not in args)
    check("...nor in security's (it prints them; it isn't given them)", KEY_ID not in seclog and KEY_SECRET not in seclog)
    check("they reach rclone only in its environment, as an env-defined remote",
          f"RCLONE_CONFIG_MCS3_ACCESS_KEY_ID={KEY_ID}" in renv and f"RCLONE_CONFIG_MCS3_SECRET_ACCESS_KEY={KEY_SECRET}" in renv)
    check("rclone's environment holds nothing else of ours (no MCOMP_*, no inherited RCLONE_*)",
          "MCOMP_" not in renv and not any(l.startswith("RCLONE_") and not l.startswith(("RCLONE_CONFIG_MCS3_", "RCLONE_CONFIG="))
                                          for l in renv.splitlines()))
    conf = os.path.join(storage.state_home(), "s3", "rclone.conf")
    check("rclone.conf stays empty", os.path.getsize(conf) == 0)
    leaks = []
    for root in (storage.state_home(), os.environ["MCOMP_DATA_DUMMY"] if "MCOMP_DATA_DUMMY" in os.environ else storage.data_dir(),
                 os.environ["MCOMP_EVENTS_DIR"], os.environ["MCOMP_LOG_DIR"]):
        for dd, _, fs in os.walk(root):
            for f in fs:
                try:
                    t = open(os.path.join(dd, f), errors="replace").read()
                except OSError:
                    continue
                if KEY_ID in t or KEY_SECRET in t:
                    leaks.append(os.path.join(dd, f))
    check("no file in the registry, the data folder, the logs or the events holds a key", not leaks, leaks)
    check("rclone output is scrubbed of the keys before it's logged",
          storage.scrub(f"x {KEY_ID} y {KEY_SECRET}", (KEY_ID, KEY_SECRET)) == "x [key] y [key]")
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "storage.py"), "volume", "keys", "s3v"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    check("volume keys refuses without a terminal (keys are typed at security's own prompt, never passed)",
          r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "storage.py"), "volume", "keys", "s3v", KEY_ID],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    check("...and has no way to take a key on the command line", r.returncode != 0)
    test_rclone_pin()


def test_rclone_pin():
    ver = "9.9.9"
    cache = os.path.join(LAB, ".cache", f"rclone-v{ver}")
    shutil.rmtree(cache, ignore_errors=True)
    d = os.path.join(TMP, "pin")
    os.makedirs(os.path.join(d, "srv"))
    os.makedirs(os.path.join(d, "fake", f"rclone-v{ver}-osx-arm64"))
    fake = os.path.join(d, "fake", f"rclone-v{ver}-osx-arm64", "rclone")
    open(fake, "w").write(f"#!/bin/sh\necho 'rclone v{ver}'\n")
    os.chmod(fake, 0o755)
    zipname = f"rclone-v{ver}-osx-arm64.zip"
    subprocess.run(["zip", "-q", "-r", os.path.join(d, "srv", zipname), f"rclone-v{ver}-osx-arm64"], cwd=os.path.join(d, "fake"),
                   check=True)
    import hashlib
    digest = hashlib.sha256(open(os.path.join(d, "srv", zipname), "rb").read()).hexdigest()
    bin_digest = hashlib.sha256(open(fake, "rb").read()).hexdigest()       # the program inside, pinned too
    gh = tempfile.mkdtemp(prefix="mcst-g", dir="/tmp")
    try:
        def gpg(*a, **k):
            return subprocess.run(["gpg", "--batch", "--homedir", gh, *a], capture_output=True, text=True, **k)
        gpg("--passphrase", "", "--quick-gen-key", "metacomputer test signer", "ed25519", "sign", "never")
        gpg("--passphrase", "", "--quick-gen-key", "metacomputer other signer", "ed25519", "sign", "never")
        fprs = [l.split(":")[9] for l in gpg("--with-colons", "--list-keys").stdout.splitlines() if l.startswith("fpr:")]
        good, other = fprs[0], fprs[1]
        open(os.path.join(d, "sums.txt"), "w").write(f"{digest}  {zipname}\n{'0' * 64}  rclone-v{ver}-linux-arm64.zip\n")

        def sign(by, out):
            gpg("--yes", "--local-user", by, "--clearsign", "-o", out, os.path.join(d, "sums.txt"))

        def keys(fpr, out):
            open(out, "w").write(gpg("--armor", "--export", fpr).stdout)

        import http.server
        import threading

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=os.path.join(d, "srv"), **k)

            def log_message(self, *_):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def fetch(pinned_digest=digest, fpr=good, bin_pin=bin_digest):
            cfg = os.path.join(d, "pins.env")
            open(cfg, "w").write(f"RCLONE_VERSION={ver}\nRCLONE_BASE_URL={url}\nRCLONE_ZIP={zipname}\n"
                                 f"RCLONE_SHA256={pinned_digest}\nRCLONE_BIN_SHA256={bin_pin}\n"
                                 f"RCLONE_KEYS_URL={url}/KEYS\nRCLONE_SIGNING_KEY_FPR={fpr}\n"
                                 f"RUN_DIR={d}/run\nLOG_DIR={d}/logs\n")
            shutil.rmtree(cache, ignore_errors=True)
            return subprocess.run([os.path.join(LAB, "scripts", "fetch-artifacts.sh"), "rclone"], capture_output=True,
                                  text=True, env=dict(os.environ, MCOMP_CONFIG=cfg), timeout=120)

        sign(good, os.path.join(d, "srv", "SHA256SUMS"))
        keys(good, os.path.join(d, "srv", "KEYS"))
        r = fetch()
        b = os.path.join(cache, "rclone")
        check("fetch-artifacts.sh rclone installs a release signed by the pinned key with the pinned digests (zip "
              "and program), and writes no digest file beside the program", r.returncode == 0 and os.path.isfile(b)
              and not os.path.exists(b + ".sha256"), r.stdout[-300:] + r.stderr[-300:])
        if os.path.isfile(b):
            check("...and storage accepts it (against the pinned program digest)",
                  storage.verify_rclone(b, ver, bin_digest) == b)
            open(b, "a").write("# changed\n")
            check("a changed rclone binary is refused (its digest isn't the pinned one)",
                  raises(storage.verify_rclone, b, ver, bin_digest) is not None)
            check("so is one that reports another version", raises(storage.verify_rclone, b, "1.0.0") is not None)
        r = fetch(bin_pin="2" * 64)
        check("a zip whose program isn't the pinned one (RCLONE_BIN_SHA256) is refused, and nothing is installed",
              r.returncode != 0 and "RCLONE_BIN_SHA256" in (r.stdout + r.stderr) and not os.path.exists(b),
              r.stderr[-300:])
        r = fetch(pinned_digest="1" * 64)
        check("a pinned digest that differs from the signed SHA256SUMS is refused", r.returncode != 0
              and "differs" in (r.stdout + r.stderr) and not os.path.exists(b), r.stderr[-300:])
        sign(other, os.path.join(d, "srv", "SHA256SUMS"))
        keys(other, os.path.join(d, "srv", "KEYS"))
        r = fetch()
        check("SHA256SUMS signed by another key (and a key file without the pinned key) is refused",
              r.returncode != 0 and not os.path.exists(b), r.stderr[-300:])
        sign(good, os.path.join(d, "srv", "SHA256SUMS"))
        keys(good, os.path.join(d, "srv", "KEYS"))
        with open(os.path.join(d, "srv", zipname), "ab") as f:
            f.write(b"tampered")
        r = fetch()
        check("a zip that doesn't match its digest is refused", r.returncode != 0 and "mismatch" in (r.stdout + r.stderr)
              and not os.path.exists(b), r.stderr[-300:])
        srv.shutdown()
    finally:
        subprocess.run(["gpgconf", "--homedir", gh, "--kill", "all"], capture_output=True)
        shutil.rmtree(gh, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def s3_fixture(name, prefix="team"):
    quiet(storage.cmd_volume_create, Args(name=name, s3=True, bucket="my-bucket", endpoint="http://127.0.0.1:9",
                                          region=None, prefix=prefix, dir=None, size=None, folder=None))
    return storage.reg_load("volume", name)


def fake_rclone(name, script):
    """A stand-in rclone for one volume, logging to that volume's own state folder (the only place, with its
    local copy, a sandboxed rclone may write)."""
    d = storage.s3_dir(name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(TMP, f"fake-rclone-{name}")
    log = os.path.join(d, "fake-args.log")
    open(path, "w").write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + log + "\n" + script)
    os.chmod(path, 0o755)
    return path, log


def test_s3_quarantine_failed_run():
    """Audit F9: what a failed run brought down reaches the shared copy too, and only with the quarantine
    attribute (the exchange marks it before it has its name there)."""
    rec, shared, sync = s3_stage("fq")
    r, _ = sync_with(rec, 'case "$1" in bisync) echo partial > "$2/partial.txt"\n'
                          '  echo "ERROR : other.bin: failed" >&2; exit 1 ;; esac\nexit 0\n')
    p = os.path.join(shared, "partial.txt")
    q = storage.get_xattr(p, storage.QUARANTINE) if os.path.exists(p) else None
    check("a file a failed sync brought down reaches the shared copy, with the quarantine attribute",
          not r["ok"] and q is not None and storage.QUAR_OK.match(q), (r.get("summary"), q))
    check("...and the failure names rclone's error line (not one character of it)", "other.bin" in r.get("error", ""),
          r.get("error"))


def test_s3_space():
    """Audit F10 (S3): no sync below the free-space reserve; transfers capped to what's above it; backups and
    entries bounded."""
    rec = s3_fixture("sp")
    rcl, log = fake_rclone("sp", "exit 0\n")
    if not hasattr(storage, "free_bytes_path"):
        check("S3 sync checks the Mac's free space (free_bytes_path)", False)
        return
    real = storage.free_bytes_path
    storage.free_bytes_path = lambda p: 1 << 20
    try:
        r = storage.s3_sync(rec, reason="manual", rclone=rcl, security=FAKE_SEC_PATH[0], resync=True)
    finally:
        storage.free_bytes_path = real
    ran = os.path.exists(log) and "bisync" in open(log).read()
    check("below the free-space reserve, the sync doesn't run, and says why", not r["ok"] and not ran
          and "free" in r.get("summary", ""), (r.get("summary"), ran))
    storage.free_bytes_path = lambda p: (12 << 30) + (5 << 20)
    try:
        r = storage.s3_sync(rec, reason="manual", rclone=rcl, security=FAKE_SEC_PATH[0], resync=True)
    finally:
        storage.free_bytes_path = real
    args = open(log).read() if os.path.exists(log) else ""
    check("above it, transfers are capped to this sync's claim: half the space above the reserve (--max-transfer)",
          f"--max-transfer {((2 << 30) + (5 << 20)) // 2}B" in args, args[-300:])
    bk = os.path.join(storage.s3_dir("sp"), "backup")
    os.makedirs(bk, exist_ok=True)
    for i in range(6):
        with open(os.path.join(bk, f"b{i}"), "wb") as f:
            f.write(b"x" * (1 << 20))
        os.utime(os.path.join(bk, f"b{i}"), (time.time() - 1000 + i, time.time() - 1000 + i))
    os.environ["MCOMP_S3_BACKUP_MAX_GB"] = str(3 / 1024)
    try:
        storage.s3_sync(rec, reason="manual", rclone=rcl, security=FAKE_SEC_PATH[0])
    finally:
        os.environ.pop("MCOMP_S3_BACKUP_MAX_GB", None)
    kept = sorted(os.listdir(bk))
    check("backups are pruned to S3_BACKUP_MAX_GB, newest kept", kept == ["b3", "b4", "b5"], kept)
    for i in range(15):
        open(os.path.join(rec["path"], f"many{i}"), "w").close()
    os.environ["MCOMP_WATCH_MAX"] = "10"
    try:
        r = storage.s3_sync(rec, reason="manual", rclone=rcl, security=FAKE_SEC_PATH[0])
    finally:
        os.environ.pop("MCOMP_WATCH_MAX", None)
    check("a local copy over the entry limit fails the sync, and says so", not r["ok"]
          and "files" in r.get("summary", ""), r.get("summary"))


def test_s3_no_auto_resync():
    """Audit F11: when rclone asks for a resync, it's never run on its own; the first sync of a copy that
    already holds files needs it asked for too."""
    rec = s3_fixture("rs")
    rcl, log = fake_rclone("rs", 'case "$*" in *--resync*) exit 0 ;; esac\n'
                                 'echo "ERROR : Bisync aborted. Must run --resync to recover." >&2\nexit 2\n')
    os.makedirs(os.path.join(storage.s3_dir("rs"), "bisync"), exist_ok=True)
    open(os.path.join(storage.s3_dir("rs"), "bisync", "x.path1.lst"), "w").close()   # not the first sync
    r = storage.s3_sync(rec, reason="interval", rclone=rcl, security=FAKE_SEC_PATH[0])
    runs = [l for l in open(log).read().splitlines() if "bisync" in l]
    check("rclone asking for a resync: no resync is run on its own", len(runs) == 1 and "--resync" not in runs[0], runs)
    check("...the sync fails and says how to resync when you choose to", not r["ok"]
          and "--resync" in r.get("summary", ""), r.get("summary"))
    rec2 = s3_fixture("fs")
    open(os.path.join(rec2["path"], "already-here.txt"), "w").write("x")
    rcl2, log2 = fake_rclone("fs", "exit 0\n")
    r = storage.s3_sync(rec2, reason="start", rclone=rcl2, security=FAKE_SEC_PATH[0])
    ran = os.path.exists(log2) and "--resync" in open(log2).read()
    check("the first sync of a local copy that already holds files waits to be asked (it could overwrite)",
          not r["ok"] and not ran and "--resync" in r.get("summary", ""), (r.get("summary"), ran))
    if "resync" in storage.s3_sync.__code__.co_varnames:
        r = storage.s3_sync(rec2, reason="manual", rclone=rcl2, security=FAKE_SEC_PATH[0], resync=True)
        args = open(log2).read()
        check("asked for, it runs with the bucket-side backup folder too", r["ok"] and "--resync" in args
              and "--backup-dir2" in args, args[-300:])


def test_s3_prepare_overflow():
    """Audit F7 (S3): a read-write S3 copy that the first sync grows past the watcher's limit refuses the start."""
    rec = s3_fixture("ov")
    os.environ["MCOMP_VOLUMES"] = "ov:rw"
    real_sync, real_kc = storage.s3_sync, storage.keychain_get

    def grow(rec_, **kw):
        for i in range(20):
            open(os.path.join(rec_["path"], f"dl{i}"), "w").close()
        return {"ok": True, "summary": "synced (stand-in)", "conflicts": []}
    storage.s3_sync, storage.keychain_get = grow, (lambda *a, **k: True)
    os.environ["MCOMP_WATCH_MAX"] = "10"
    try:
        e = raises(storage.cmd_prepare, Args(pid=os.getpid(), no_volumes=False))
    finally:
        storage.s3_sync, storage.keychain_get = real_sync, real_kc
        os.environ.pop("MCOMP_WATCH_MAX", None)
        storage.release_locks(storage.vm_name(), storage.lab_root())
        storage.clear_state()
        os.environ["MCOMP_VOLUMES"] = ""
    check("an S3 copy the first sync grew past the watcher's limit refuses the start", e is not None, e)


# --- the recheck (round 2): each test fails against the code before it ---------------------------
def bound(rec):
    """A registry record with the identity of the file at its path, as `create` records it."""
    st = os.stat(rec["path"])
    return dict(rec, ident=[st.st_dev, st.st_ino])


def synth(kind, name, path, rw, exclusive=None):
    """An item as a plan makes it, without the plan's own checks (to test the lock rules alone)."""
    real = storage.canon_or_abs(path)
    lk = "folder" if kind in ("folder", "s3") else kind
    return {"kind": kind, "name": name, "path": real, "rw": rw, "lock": storage.lock_key(lk, real),
            "exclusive": rw if exclusive is None else exclusive}


def test_overlap_order():
    """Recheck P1 #3 (F5, F12): overlap decisions don't depend on order, cover a plan's own items, reference
    disks and running operations; a folder holding images can't be shared at all; an image is never reached
    through a link; a new image can't go into a folder volume."""
    pid = os.getpid()
    base = os.path.join(TMP, "order")
    parent = os.path.join(base, "parent.noindex")
    sub = os.path.join(parent, "sub")
    os.makedirs(sub)
    img = os.path.join(parent, "vol.img")
    with open(img, "wb") as f:
        f.truncate(16 << 20)
    rec = bound({"name": "ordvol", "kind": "disk", "path": img, "uuid": "11111111-2222-4333-8444-666666666666",
                 "created": "x"})
    storage.reg_save("volume", rec)
    W = synth("folder", "around", parent, True)
    with storage.reserved([rec], "checkpoint"):
        e = raises(storage.acquire, [W], "vm-late", "/lab", pid)
    storage.release_locks("vm-late", "/lab")
    check("while an operation holds a disk image, a read-write folder around it is refused (the order that "
          "lets the guest swap the image mid-checkpoint)", e is not None, e)
    got = storage.acquire([W], "vm-late", "/lab", pid)
    check("(without the reservation, the same item is taken: the refusal above was the reservation's)", bool(got), got)
    e = raises(storage.acquire, [synth("disk", "ordvol", img, True)], "vm-d", "/lab", pid)
    check("...and the disk inside it is refused while that folder is held", e is not None, e)
    storage.release_locks("vm-late", "/lab")
    storage.release_locks("vm-d", "/lab")
    inner = synth("folder", "inner", sub, False)
    for what, items in (("a read-write folder and a folder inside it", [W, inner]),
                        ("...listed the other way round", [inner, W]),
                        ("a read-write folder and a disk image inside it", [W, synth("disk", "ordvol", img, True)])):
        e = raises(storage.acquire, items, "vm-p", "/lab", pid)
        storage.release_locks("vm-p", "/lab")
        check(f"one start holding {what} is refused", e is not None, e)
    other1, other2 = os.path.join(base, "o1.noindex"), os.path.join(base, "o2")
    os.makedirs(other1)
    os.makedirs(other2)
    got = storage.acquire([synth("folder", "o1", other1, True), synth("folder", "o2", other2, False)], "vm-p2", "/lab", pid)
    storage.release_locks("vm-p2", "/lab")
    check("(a start of unrelated folders is taken)", len(got) == 2, got)
    refdir = os.path.join(base, "refs")
    os.makedirs(refdir)
    refimg = os.path.join(refdir, "r.iso")
    with open(refimg, "wb") as f:
        f.write(b"\0" * 65536)
    storage.reg_save("refdisk", bound({"name": "ordref", "kind": "refdisk", "path": refimg,
                                       "uuid": "11111111-2222-4333-8444-777777777777", "created": "x"}))
    os.environ.update(MCOMP_VOLUMES="", MCOMP_REFDISKS="ordref")
    try:
        items = storage.build_plan()[0]
        storage.acquire(items, "vm-r", "/lab", pid)
        recorded = any(r.get("kind") == "refdisk" and r.get("vm") == "vm-r" for r in storage.all_locks())
        check("a VM's reference disk gets a record of its own (shared)", recorded)
        e = raises(storage.acquire, [synth("folder", "around-ref", refdir, True)], "vm-s", "/lab", pid)
        check("...so a read-write folder around a reference disk a VM has is refused", e is not None, e)
        e2 = raises(storage.acquire, items, "vm-r2", "/lab", pid)
        check("(another VM can still attach the same reference disk)", e2 is None, e2)
    finally:
        for v in ("vm-r", "vm-r2", "vm-s"):
            storage.release_locks(v, "/lab")
        os.environ["MCOMP_REFDISKS"] = ""
    e = raises(storage.check_shareable_id, parent)
    check("a folder holding a registered disk image can't be shared at all (a VM writing there could swap it)",
          e is not None and "image" in e, e)
    check("...nor its parent", raises(storage.check_shareable_id, base) is not None)
    check("(a folder inside it, beside the image, still can)", raises(storage.check_shareable_id, sub) is None)
    private = os.path.join(base, "private.db")
    with open(private, "wb") as f:
        f.write(b"PRIVATE")
        f.truncate(16 << 20)
    os.rename(img, img + "-was")
    os.symlink(private, img)
    os.environ["MCOMP_VOLUMES"] = "ordvol"
    try:
        items, problems, _ = storage.build_plan()
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
        os.unlink(img)
        os.rename(img + "-was", img)
    check("a disk image swapped for a link to another file isn't followed: that start is refused",
          problems and not any(storage.fold(it.get("path", "")) == storage.fold(storage.canon_or_abs(private))
                               for it in items), (items, problems))
    fv = os.path.join(base, "fv.noindex")
    os.makedirs(fv)
    quiet(storage.cmd_volume_create, Args(name="ordfv", folder=fv, size=None, dir=None))
    e = raises(storage.cmd_volume_create, Args(name="ordimg", size="16M", dir=fv, folder=None, s3=False))
    check("a new image can't go into a folder volume (--dir inside one)", e is not None
          and not os.path.exists(os.path.join(fv, "ordimg.img")), e)
    for n in ("ordvol", "ordfv"):
        storage.reg_delete("volume", n)
    storage.reg_delete("refdisk", "ordref")


def test_reservation_lifetimes():
    """Recheck #12 (F12): a delete holds its reservation until the registry entry is gone; checkpoint delete takes
    its turn with save and restore of the same volumes and checkpoint."""
    rec = make_volume("lifevol")
    seen = []
    real = storage.reg_delete

    def spy(kind, name):
        if name == "lifevol":
            seen.append(storage.holder_of(rec["path"], "disk") is not None)
        return real(kind, name)
    storage.reg_delete = spy
    try:
        quiet(storage.cmd_volume_delete, Args(name="lifevol", yes=True))
    finally:
        storage.reg_delete = real
    check("volume delete holds its reservation until the registry entry is gone", seen and all(seen), seen)
    rec = make_volume("ckvol")
    os.environ["MCOMP_VOLUMES"] = "ckvol"
    try:
        quiet(storage.cmd_checkpoint, Args(op="save", name="lc1"))
        man = storage.ckpt_manifest(storage.vm_name(), "lc1")
        snap = storage.snapshot_path(rec["path"], storage.vm_name(), "lc1")
        with storage.reserved([rec], "restore"):
            e = raises(storage.cmd_checkpoint, Args(op="delete", name="lc1"))
        check("checkpoint delete is refused while a restore holds its volume (it would pull the snapshot away)",
              e is not None and os.path.exists(man) and os.path.exists(snap), e)
        if hasattr(storage, "ckpt_item"):
            with storage.reserved([], "restore", [storage.ckpt_item(storage.vm_name(), "lc1")]):
                e = raises(storage.cmd_checkpoint, Args(op="delete", name="lc1"))
            check("...and while another operation holds the checkpoint itself", e is not None and os.path.exists(man), e)
        else:
            check("...and while another operation holds the checkpoint itself", False, "no checkpoint lock")
        quiet(storage.cmd_checkpoint, Args(op="delete", name="lc1"))
        check("(with nothing else holding them, it deletes)", not os.path.exists(man) and not os.path.exists(snap))
    finally:
        os.environ["MCOMP_VOLUMES"] = ""


def test_link_rules():
    """Recheck #4: a link is kept only when its text can't lead out of the folder, chains included."""
    root = os.path.join(TMP, "links.noindex")
    os.makedirs(os.path.join(root, "sub"))
    outside = os.path.join(TMP, "outside")
    os.makedirs(outside, exist_ok=True)
    open(os.path.join(outside, "secret"), "w").write("outside the share\n")
    open(os.path.join(root, "f"), "w").write("inside\n")
    open(os.path.join(root, "sub", "f"), "w").write("inside\n")
    links = {"a": ".", "leak": "a/../outside", "abs": root + "/../../private", "ok": "sub/f", "sub/up": "../f",
             "sub/up2": "../../outside/secret", "sub/dotted": "./../sub/f"}
    for rel, target in links.items():
        os.symlink(target, os.path.join(root, rel))
    check("(the chain really leads out: leak resolves outside the folder)",
          not storage.inside(os.path.realpath(os.path.join(root, "leak")), os.path.realpath(root)))
    try:
        protect_once(root)
    except Exception as e:  # noqa: BLE001
        print(f"  (the pass raised {e!r})")
    gone = {rel for rel in links if not os.path.lexists(os.path.join(root, rel))}
    check("a ..-step after a name (a -> ., leak -> a/../outside) is removed", "leak" in gone, gone)
    check("an absolute target is removed, even one spelled from the folder's own path (ROOT/../..)", "abs" in gone, gone)
    check("climbing above the folder from a subfolder is removed", "sub/up2" in gone, gone)
    check("links that can only stay inside are kept (a -> ., ok -> sub/f, sub/up -> ../f, ./../sub/f)",
          not gone & {"a", "ok", "sub/up", "sub/dotted"}, gone)


def test_quoted_keys():
    """Recheck #10 (F13): a quoted key is its exact text; one that differs from a known key by a space or an
    invisible character grants nothing."""
    m = 'resources:\n  "files ":\n    write:\n      - {dir: /data, recursive: true}\n'

    def grants(text):
        try:
            return storage.manifest_dirs(text)[1]
        except storage.ManifestError:
            return []
    check('the quoted key "files " grants nothing (it isn\'t files)', grants(m) == [], grants(m))
    check("(the same manifest with files: grants /data)", grants(m.replace('"files "', "files")) == [("/data", True)])
    check('("files" quoted exactly is files)', grants(m.replace('"files "', '"files"')) == [("/data", True)])
    for odd in ('" files"', "'files​'", '"fi\\\\les"', '"resources"'):
        text = m.replace('"files "', odd)
        check(f"an odd key ({odd}) grants nothing", grants(text) == [], grants(text))
    flow = 'resources:\n  files:\n    write:\n      - {"dir ": /data, recursive: true}\n'
    check('a flow mapping\'s "dir " key isn\'t dir', grants(flow) == [], grants(flow))


def test_copy_bounds():
    """Recheck #11: a copy that can't be a clone (another filesystem) stays sparse, stops at the size that was
    checked, and packing a reference disk is refused when its files, at full size, don't fit above the reserve."""
    dmg, mnt = os.path.join(TMP, "xdev.dmg"), os.path.join(TMP, "xdev-mnt")
    os.makedirs(mnt)
    r = subprocess.run(["hdiutil", "create", "-quiet", "-size", "64m", "-fs", "APFS", "-volname", "mcxdev", dmg],
                       capture_output=True, text=True)
    r2 = subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", mnt, dmg], capture_output=True,
                        text=True) if r.returncode == 0 else r
    if r2.returncode != 0:
        print(f"SKIP the cross-filesystem copy checks (hdiutil: {(r.stderr + r2.stderr).strip()[:200]})")
        return
    try:
        src = os.path.join(mnt, "sparse.bin")
        with open(src, "wb") as f:
            f.truncate(2 << 30)
            f.seek(1 << 30)
            f.write(b"D" * 4096)
        out = os.path.join(TMP, "xdev-out")
        os.makedirs(out)
        dfd = os.open(out, storage.DIR_NOFOLLOW)
        sfd = os.open(src, storage.FILE_NOFOLLOW)
        try:
            check("(the source and the copy are on different filesystems)",
                  os.fstat(sfd).st_dev != os.fstat(dfd).st_dev)
            storage.copy_from_fd(sfd, dfd, "copy.bin", os.fstat(sfd))
        finally:
            os.close(sfd)
        cst = os.stat(os.path.join(out, "copy.bin"))
        check("across filesystems, a sparse file's copy stays sparse (2 GiB long, a few KiB used)",
              cst.st_size == 2 << 30 and cst.st_blocks * 512 < 64 << 20, (cst.st_size, cst.st_blocks * 512))
        grow = os.path.join(mnt, "grow.bin")
        with open(grow, "wb") as f:
            f.write(b"a" * (1 << 20))
        sfd = os.open(grow, storage.FILE_NOFOLLOW)
        try:
            st = os.fstat(sfd)
            with open(grow, "ab") as f:
                f.write(b"b" * (1 << 20))              # the guest, between the check and the copy
            storage.copy_from_fd(sfd, dfd, "grow.bin", st)
        finally:
            os.close(sfd)
            os.close(dfd)
        check("...and copies no further than the size that was checked",
              os.path.getsize(os.path.join(out, "grow.bin")) == 1 << 20, os.path.getsize(os.path.join(out, "grow.bin")))
        pack = os.path.join(mnt, "pack")
        os.makedirs(pack)
        with open(os.path.join(pack, "huge.bin"), "wb") as f:
            f.truncate(2 << 30)
        free = storage.free_bytes_path(storage.data_dir() if os.path.isdir(storage.data_dir()) else TMP)
        os.environ["MCOMP_IMPORT_MIN_FREE_GB"] = str((free - (1 << 30)) / (1 << 30))
        try:
            e = raises(storage.cmd_refdisk_create, Args(name="xbig", src=pack, dir=None))
        finally:
            os.environ.pop("MCOMP_IMPORT_MIN_FREE_GB", None)
        iso = os.path.join(storage.data_dir(), "refdisks", "xbig.iso")
        check("a reference disk whose files (at full size) don't fit above the reserve is refused before packing",
              e is not None and not os.path.exists(iso), e)
        if os.path.exists(iso):
            os.chmod(iso, 0o600)
            os.unlink(iso)
            storage.reg_delete("refdisk", "xbig")
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", "-force", mnt], capture_output=True)


class _FakeEntry:
    def __init__(self, name):
        self.name = name


def test_watch_bounds():
    """Recheck #8 (F7): a folder is counted while it's listed, a pass is bounded in time, and the supervisor
    checks free space on its own."""
    yields = [0]

    class Endless:
        def __init__(self):
            self.i = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.i += 1
            yields[0] += 1
            if self.i > 100000:
                raise StopIteration
            return _FakeEntry(f"e{self.i}")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass
    fd = os.open(TMP, os.O_RDONLY | os.O_DIRECTORY)
    real = os.scandir
    os.scandir = lambda arg=None: Endless()
    try:
        try:
            storage.walk_fd(fd, lambda *a: None, limit=10)
        except storage.ProtectionFailure:
            pass
    finally:
        os.scandir = real
    check("a huge folder fails at the limit while it's listed (not after all of it is read)", yields[0] <= 11, yields[0])
    try:
        storage.walk_fd(fd, lambda *a: None, deadline=time.monotonic() - 1)
        ok = False
    except storage.ProtectionFailure:
        ok = True
    except TypeError:
        ok = False
    check("a pass that runs past its time limit fails (the VM is stopped)", ok)
    if hasattr(storage, "supervise"):
        r, w = os.pipe()
        real_free = storage.free_bytes_at
        storage.free_bytes_at = lambda f: 1 << 20
        try:
            t0 = time.monotonic()
            why = storage.supervise(r, 0, "", [fd])
            took = time.monotonic() - t0
        finally:
            storage.free_bytes_at = real_free
            os.close(r)
            os.close(w)
        check("the supervisor checks free space on its own, twice a second, and stops the VM below the reserve",
              why and "free" in why and took < 3, (why, took))
    else:
        check("the supervisor checks free space on its own", False, "no supervisor")
    os.close(fd)


def watcher_fixture(name):
    root = os.path.join(TMP, f"{name}.noindex")
    os.makedirs(root)
    quiet(storage.cmd_volume_create, Args(name=name, folder=root, size=None, dir=None))
    os.environ["MCOMP_VOLUMES"] = f"{name}:rw"
    with open(os.path.join(TMP, "ssh", "guest-features"), "w") as f:
        f.write("storage=1\n")
    vm = subprocess.Popen(["sleep", "120"])
    open(os.path.join(TMP, "run", "lume.pid"), "w").write(str(vm.pid))
    quiet(storage.cmd_prepare, Args(pid=os.getpid(), no_volumes=False))
    return root, vm


def watcher_cleanup(vm, *procs):
    for p in procs:
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGCONT)
            except OSError:
                pass
            p.kill()
            p.wait()
    if vm.poll() is None:
        vm.kill()
    vm.wait()
    storage.release_locks(storage.vm_name(), storage.lab_root())
    storage.clear_state()
    os.environ["MCOMP_VOLUMES"] = ""


def vm_ends(vm, seconds):
    try:
        vm.wait(seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_fail_closed():
    """Recheck #5 and #8: the VM is stopped first, before reporting (a full disk can't skip it); a watcher that
    dies or gets stuck is caught by its supervisor, which stops the VM."""
    root, vm = watcher_fixture("wfull")
    try:
        for i in range(40):
            open(os.path.join(root, f"f{i}"), "w").close()
        wrapper = ("import errno, sys\nsys.path.insert(0, %r)\nimport storage\n"
                   "def full(*a, **k):\n    raise OSError(errno.ENOSPC, 'No space left on device')\n"
                   "storage.atomic_json = full\nstorage.emit = full\nsys.exit(storage.main(['watch']))\n"
                   % os.path.join(LAB, "lib"))
        r = subprocess.run([sys.executable, "-c", wrapper], env=dict(os.environ, MCOMP_WATCH_MAX="20"),
                           capture_output=True, text=True, timeout=60)
        check("with the Mac's disk full (writing the report fails), a protection failure still stops the VM",
              vm_ends(vm, 15), (r.returncode, r.stderr[-300:]))
    finally:
        watcher_cleanup(vm)
    for how in ("killed", "stuck"):
        root, vm = watcher_fixture(f"wsup{how}")
        w = None
        try:
            env = dict(os.environ, MCOMP_WATCH_STALL_MAX="2")
            w = subprocess.Popen([sys.executable, os.path.join(LAB, "lib", "storage.py"), "watch"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            check(f"(the watcher is running before it's {how})", w.poll() is None)
            w.send_signal(signal.SIGKILL if how == "killed" else signal.SIGSTOP)
            check(f"a watcher that's {how} is caught by its supervisor, which stops the VM",
                  vm_ends(vm, 20))
        finally:
            watcher_cleanup(vm, w)


def detached(cmd):
    """Start cmd outside this process's children (so it's reaped as soon as it ends: a zombie would still look
    alive to kill -0); returns its pid."""
    out = subprocess.run(["bash", "-c", " ".join(f"'{c}'" for c in cmd) + " >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True).stdout
    return int(out.strip())


def pid_gone(pid, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def test_host_side_stop():
    """Recheck #6 (F6): a storage-security failure powers the VM off from the Mac, never asking the guest; and an
    ordinary stop has a deadline whatever the guest does with its shutdown request."""
    d = os.path.join(TMP, "stop6")
    os.makedirs(os.path.join(d, "run"))
    os.makedirs(os.path.join(d, "logs"))
    cfg = os.path.join(d, "test.env")
    open(cfg, "w").write(f"RUN_DIR={d}/run\nLOG_DIR={d}/logs\nVM_NAME=stoptest\n")
    open(os.path.join(d, "run", "storage-state.json"), "w").write("{}")
    calls = os.path.join(d, "calls.log")
    for what in ("abort", "stop"):
        vmpid = detached([sys.executable, "-c", "import time; time.sleep(600)", "run", "stoptest"])
        open(os.path.join(d, "run", "lume.pid"), "w").write(str(vmpid))
        lume = os.path.join(d, "lume")
        open(lume, "w").write(f'#!/bin/sh\necho "lume $*" >> "{calls}"\n'
                              f'case "$1" in stop) kill {vmpid} ;; esac\nexit 0\n')
        os.chmod(lume, 0o755)
        act = "storage_abort" if what == "abort" else "MCOMP_STOP_DEADLINE=3 vm_stop stoptest"
        script = (f'. "{LAB}/scripts/lib.sh"\ndie() {{ exit 1; }}\nLUME="{lume}"\n'
                  f'vm_running() {{ kill -0 {vmpid} 2>/dev/null; }}\n'
                  f'gssh() {{ echo gssh >> "{calls}"; sleep 600; }}\nnetgw_stop() {{ :; }}\nstorage_release() {{ :; }}\n'
                  f'eval "$(sed -n \'/^storage_abort()/,/^}}/p\' "{LAB}/bin/metacomputer")"\n{act}\necho done >> "{calls}"\n')
        open(calls, "w").close()
        p = subprocess.Popen(["bash", "-c", script], env=dict(os.environ, MCOMP_CONFIG=cfg), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        t0 = time.time()
        try:
            p.wait(45)
            finished = True
        except subprocess.TimeoutExpired:
            finished = False
        took = time.time() - t0
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        log = open(calls).read()
        stopped = pid_gone(vmpid, 5)
        if what == "abort":
            check("storage_abort powers the VM off from the Mac without asking the guest (a stalling guest can't keep it "
                  "running)", finished and stopped and "gssh" not in log, (finished, stopped, round(took), log))
        else:
            check("an ordinary stop gives up on a guest that never powers off, within its deadline", finished and stopped
                  and took < 30, (finished, stopped, round(took), log))
        try:
            os.kill(vmpid, signal.SIGKILL)
        except OSError:
            pass


# --- S3, two stages ----------------------------------------------------------------------------------
def s3_stage(name):
    rec = s3_fixture(name)
    return rec, rec["path"], os.path.join(storage.s3_dir(name), "sync")


def sync_with(rec, script, **kw):
    """One s3_sync with a stand-in rclone running script ($1 is rclone's command, $2 its first path)."""
    rcl, log = fake_rclone(rec["name"], '[ "$1" = --color ] && shift 2\n' + script)
    kw.setdefault("resync", True)
    r = storage.s3_sync(rec, reason="manual", rclone=rcl, security=FAKE_SEC_PATH[0], **kw)
    return r, (open(log).read() if os.path.exists(log) else "")


def test_s3_two_stages():
    """Recheck P1 #1 and #2 (F1, F9): rclone works only on a private sync copy; the shared copy is reached
    through its own checked descriptor, and what comes from the bucket is quarantined before it has its name
    there; a replaced copy (or a replaced parent) isn't synced, and nothing private is read, marked or sent."""
    rec, shared, sync = s3_stage("two")
    open(os.path.join(shared, "from-vm.txt"), "w").write("written in the VM\n")
    private = os.path.join(TMP, "private-two")
    os.makedirs(private)
    open(os.path.join(private, "secret.txt"), "w").write("PRIVATE-MAC-FILE\n")
    os.symlink(os.path.join(private, "secret.txt"), os.path.join(shared, "link-out"))
    check("(the VM's link in the shared copy leads to a private file)",
          open(os.path.join(shared, "link-out")).read().startswith("PRIVATE"))
    published = []
    real_rx = getattr(storage, "rename_excl", None)

    def watch_rename(fromfd, old, tofd, new):
        fd = os.open(old, storage.FILE_NOFOLLOW, dir_fd=fromfd)
        try:
            v = storage.fget_xattr(fd, storage.QUARANTINE)
            published.append((new, storage.fd_path(tofd), bool(v and storage.QUAR_OK.match(v))))
        finally:
            os.close(fd)
        return real_rx(fromfd, old, tofd, new)
    if real_rx:
        storage.rename_excl = watch_rename
    script = (f'S="$2"\ncase "$1" in bisync)\n'
              f'  test -f "$S/from-vm.txt" && echo SEEN-BEFORE >> "{storage.s3_dir("two")}/fake-args.log"\n'
              f'  echo "from the bucket" > "$S/from-bucket.txt" ;; esac\nexit 0\n')
    try:
        r, log = sync_with(rec, script)
    finally:
        if real_rx:
            storage.rename_excl = real_rx
    args = [l for l in log.splitlines() if " bisync " in f" {l} "]
    check("rclone is never given the shared copy (only the private sync copy)", args and all(
        shared not in a for a in args) and all(sync in a for a in args), args)
    check("the VM's new file is in the sync copy before rclone runs", "SEEN-BEFORE" in log, log[-300:])
    got = os.path.join(shared, "from-bucket.txt")
    v = storage.get_xattr(got, storage.QUARANTINE) if os.path.exists(got) else None
    check("a file from the bucket reaches the shared copy, with the quarantine attribute", r["ok"] and v is not None
          and storage.QUAR_OK.match(v), (r.get("summary"), v))
    into_shared = [p for p in published if storage.inside(p[1], storage.canon_or_abs(shared))]
    check("...which it had before it got its name there (never visible without it)", into_shared
          and all(q for _, _, q in into_shared), published)
    check("the VM's link out isn't followed: nothing private reaches the sync copy",
          not any("PRIVATE" in open(os.path.join(dp, f), errors="replace").read()
                  for dp, _, fs in os.walk(sync) for f in fs))
    # the copy replaced by a link to a private folder (another VM with its parent read-write, between syncs)
    runs = lambda text: text.count(" bisync ")  # noqa: E731
    before = runs(log)
    os.rename(shared, shared + "-was")
    os.symlink(private, shared)
    try:
        r, log = sync_with(rec, "exit 0\n")
    finally:
        os.unlink(shared)
        os.rename(shared + "-was", shared)
    check("a shared copy replaced by a link isn't synced (rclone isn't run)", not r["ok"] and runs(log) == before,
          r.get("summary"))
    check("...the private folder's files get no attribute", storage.get_xattr(os.path.join(private, "secret.txt"),
                                                                               storage.QUARANTINE) is None)
    check("...and nothing of it reaches the sync copy", not os.path.exists(os.path.join(sync, "secret.txt")))
    # a parent replaced: STORAGE_DIR/volumes moved away and a link put in its place, to a folder with the same name
    vols = os.path.dirname(shared)
    fake = os.path.join(TMP, "fake-vols")
    os.makedirs(os.path.join(fake, os.path.basename(shared)))
    open(os.path.join(fake, os.path.basename(shared), "planted.txt"), "w").write("PRIVATE elsewhere\n")
    os.rename(vols, vols + "-was")
    os.symlink(fake, vols)
    try:
        r, log = sync_with(rec, "exit 0\n")
    finally:
        os.unlink(vols)
        os.rename(vols + "-was", vols)
    check("a shared copy whose parent was replaced (same path, another folder) isn't synced", not r["ok"]
          and not os.path.exists(os.path.join(sync, "planted.txt")), r.get("summary"))
    # another VM holds a read-write folder around the copy: no sync until it's gone
    storage.acquire([synth("folder", "around", vols, True)], "vm-around", "/lab", os.getpid())
    try:
        r, log = sync_with(rec, "exit 0\n")
    finally:
        storage.release_locks("vm-around", "/lab")
    check("no sync while another VM has a read-write folder around the copy (it could swap the copy meanwhile)",
          not r["ok"], r.get("summary"))


def test_s3_exchange():
    """The two copies stay in step: deletions both ways, a conflict keeps both, and a bucket with more entries
    than the limit brings nothing in (whether the VM has the volume read-only or read-write)."""
    rec, shared, sync = s3_stage("xch")
    open(os.path.join(shared, "keep.txt"), "w").write("v1\n")
    open(os.path.join(shared, "gone-in-vm.txt"), "w").write("x\n")
    r, _ = sync_with(rec, f'case "$1" in bisync) echo bucket > "$2/gone-in-bucket.txt" ;; esac\nexit 0\n')
    r2, _ = sync_with(rec, 'exit 0\n')
    check("(both copies hold the files after a first sync)", r["ok"] and r2["ok"] and all(
        os.path.exists(os.path.join(p, n)) for p in (shared, sync) for n in ("keep.txt", "gone-in-vm.txt",
                                                                             "gone-in-bucket.txt")), (r, r2))
    open(os.path.join(storage.s3_dir("xch"), "bisync", "x.path1.lst"), "w").close()     # not a first sync now
    os.unlink(os.path.join(shared, "gone-in-vm.txt"))
    # While rclone runs, the bucket's side changes keep.txt (an older time) and the VM changes it too (newer).
    script = ('case "$1" in bisync)\n  rm -f "$2/gone-in-bucket.txt"\n'
              '  printf "changed in the bucket\\n" > "$2/keep.txt"; touch -t 200001010000 "$2/keep.txt" ;; esac\nexit 0\n')
    real_run = storage.run_rclone

    def run_and_vm_writes(*a, **k):
        out = real_run(*a, **k)
        with open(os.path.join(shared, "keep.txt"), "w") as f:
            f.write("changed in the VM\n")
        return out
    storage.run_rclone = run_and_vm_writes
    try:
        r, log = sync_with(rec, script, resync=False)
    finally:
        storage.run_rclone = real_run
    check("a file deleted in the VM is deleted from the sync copy", not os.path.exists(os.path.join(sync, "gone-in-vm.txt")))
    check("a file deleted in the bucket is deleted from the shared copy",
          not os.path.exists(os.path.join(shared, "gone-in-bucket.txt")))
    r2, _ = sync_with(rec, "exit 0\n", resync=False)
    names = sorted(os.listdir(shared))
    check("a file changed on both sides keeps both: the newer under its name, the other as NAME.conflict1.EXT, in both "
          "copies", open(os.path.join(shared, "keep.txt")).read() == "changed in the VM\n" and "keep.conflict1.txt" in names
          and os.path.exists(os.path.join(sync, "keep.conflict1.txt")) and "keep.txt" in r.get("conflicts", []),
          (names, r.get("conflicts"), r.get("summary")))
    rec2, shared2, sync2 = s3_stage("flood")
    os.environ["MCOMP_WATCH_MAX"] = "20"
    try:
        r, _ = sync_with(rec2, 'case "$1" in bisync) i=0; while [ $i -lt 40 ]; do : > "$2/f$i"; i=$((i+1)); done ;; esac\n'
                               'exit 0\n')
    finally:
        os.environ.pop("MCOMP_WATCH_MAX", None)
    inside_now = os.listdir(shared2)
    unq = [n for n in inside_now if storage.get_xattr(os.path.join(shared2, n), storage.QUARANTINE) is None]
    check("a bucket with more entries than the limit brings nothing in, and says so", not r["ok"] and not inside_now
          and "files" in r.get("summary", ""), (r.get("summary"), inside_now[:5]))
    check("...and nothing in the shared copy is left without the attribute", not unq, unq[:5])


def test_s3_space_claims():
    """Recheck #9 (F10): concurrent operations can't spend the same free space; backups are pruned before rclone
    runs; rclone is stopped when the disk falls below the reserve during its run."""
    rec, shared, sync = s3_stage("claim")
    os.makedirs(sync, mode=0o700, exist_ok=True)
    real = storage.free_bytes_path
    storage.free_bytes_path = lambda p: (12 << 30)          # 2 GiB above the 10 GiB reserve
    try:
        with storage.space_claims([sync], "another-sync") as held:
            r, log = sync_with(rec, "exit 0\n")
    except AttributeError:
        held, r, log = {}, {"ok": False}, ""
    finally:
        storage.free_bytes_path = real
    caps = [int(x) for x in __import__("re").findall(r"--max-transfer (\d+)B", log)]
    check("with another sync's claim held, this one's transfers are capped to what's left of the space above the "
          "reserve (half of it), not all of it again", r["ok"] and caps and caps[-1] <= (2 << 30) - list(held.values())[0],
          (caps, held))
    bk = os.path.join(storage.s3_dir("claim"), "backup")
    os.makedirs(bk, exist_ok=True)
    for i in range(6):
        with open(os.path.join(bk, f"b{i}"), "wb") as f:
            f.write(b"x" * (1 << 20))
    marker = os.path.join(storage.s3_dir("claim"), "backup-size-at-run")
    os.environ["MCOMP_S3_BACKUP_MAX_GB"] = str(2 / 1024)
    try:
        r, log = sync_with(rec, f'case "$1" in bisync) ls "{bk}" | wc -l > "{marker}" ;; esac\nexit 0\n')
    finally:
        os.environ.pop("MCOMP_S3_BACKUP_MAX_GB", None)
    n = open(marker).read().strip() if os.path.exists(marker) else "?"
    check("backups are pruned to S3_BACKUP_MAX_GB before rclone runs (it may add more)", n == "2", n)
    flag = os.path.join(storage.s3_dir("claim"), "running")
    storage.free_bytes_path = lambda p: (1 << 20) if os.path.exists(flag) else (40 << 30)
    try:
        t0 = time.time()
        r, log = sync_with(rec, f'case "$1" in bisync) : > "{flag}"; sleep 15 ;; esac\nexit 0\n')
        took = time.time() - t0
    finally:
        storage.free_bytes_path = real
    check("rclone is stopped when the disk falls below the reserve while it runs", not r["ok"] and took < 12,
          (r.get("summary"), round(took)))


def test_rclone_planted():
    """A planted rclone with a matching rclone.sha256 beside it is refused, never run, and the program is unpacked
    again from the pinned zip (the one in this checkout's download, copied to a test folder)."""
    import hashlib
    ver = storage.env("RCLONE_VERSION", "")
    zipname = storage.env("RCLONE_ZIP", "")
    z = os.path.join(LAB, ".cache", f"rclone-v{ver}", zipname)
    if not os.path.isfile(z):
        print("SKIP the planted-rclone check (needs the pinned rclone's zip: scripts/fetch-artifacts.sh rclone)")
        return
    cache = os.path.join(TMP, "rcache")
    d = os.path.join(cache, f"rclone-v{ver}")
    os.makedirs(d)
    shutil.copy(z, d)
    marker = os.path.join(TMP, "planted-rclone-ran")
    planted = os.path.join(d, "rclone")
    open(planted, "w").write(f"#!/bin/sh\ntouch '{marker}'\necho 'rclone v{ver}'\n")
    os.chmod(planted, 0o755)
    open(os.path.join(d, "rclone.sha256"), "w").write(hashlib.sha256(open(planted, "rb").read()).hexdigest() + "\n")
    try:
        got = storage.rclone_bin(fetch=False, cache=cache)
    except TypeError:                                   # before the pin: the program and the file beside it
        got = storage.verify_rclone(planted, ver)
    except storage.StorageError as e:
        got = None
        print(f"  (refused: {e})")
    pin = storage.env("RCLONE_BIN_SHA256", "")
    check("a planted rclone with a matching rclone.sha256 beside it is refused, never run", not os.path.exists(marker))
    check("...and the program is unpacked again from the pinned zip (the pinned program digest)",
          got == planted and pin and storage.file_sha256(planted) == pin
          if hasattr(storage, "file_sha256") else False, got)
    check("(no digest file is left beside it to trust)", not os.path.exists(os.path.join(d, "rclone.sha256")))


def test_s3_real_rclone():
    """With the pinned rclone itself, sandboxed as every sync runs it, and a folder in the volume's state folder
    standing in for the bucket: every option a sync passes is accepted, and files go both ways through the two
    stages (a stand-in rclone can't catch an option rclone refuses)."""
    ver = storage.env("RCLONE_VERSION", "")
    rclone = os.path.join(LAB, ".cache", f"rclone-v{ver}", "rclone")
    if not os.path.isfile(rclone):
        print("SKIP the real rclone sync (needs the pinned rclone: scripts/fetch-artifacts.sh rclone)")
        return
    rec, shared, sync = s3_stage("real")
    rec["s3"]["prefix"] = ""
    bucket = os.path.join(storage.s3_dir("real"), "bucket-standin")
    os.makedirs(bucket)
    open(os.path.join(bucket, "from-bucket.txt"), "w").write("hello from the bucket\n")
    open(os.path.join(shared, "from-vm.txt"), "w").write("written in the VM\n")
    real_remote = storage.s3_remote
    storage.s3_remote = lambda b: bucket
    try:
        # rclone=None: the sync finds the program itself (rclone_bin: checked against its pinned digest)
        r = storage.s3_sync(rec, reason="manual", rclone=None, security=FAKE_SEC_PATH[0], resync=True)
        got = os.path.join(shared, "from-bucket.txt")
        v = storage.get_xattr(got, storage.QUARANTINE) if os.path.exists(got) else None
        check("the real rclone accepts a first sync's options and both files cross (the bucket's one quarantined)",
              r["ok"] and os.path.exists(os.path.join(bucket, "from-vm.txt")) and v is not None,
              (r.get("summary"), r.get("error")))
        open(os.path.join(shared, "from-vm.txt"), "w").write("changed in the VM\n")
        r = storage.s3_sync(rec, reason="manual", rclone=rclone, security=FAKE_SEC_PATH[0])
        p = os.path.join(bucket, "from-vm.txt")
        check("...and an ordinary sync's: the VM's change reaches the bucket",
              r["ok"] and os.path.exists(p) and open(p).read() == "changed in the VM\n", (r.get("summary"), r.get("error")))
    finally:
        storage.s3_remote = real_remote


def test_s3_sandbox():
    """Recheck P1 #1 (F1): the sandbox lets rclone write only in the volume's state folder, nowhere else, even
    where this user may write; and it reads nothing private."""
    ver = storage.env("RCLONE_VERSION", "")
    rclone = os.path.join(LAB, ".cache", f"rclone-v{ver}", "rclone")
    if not os.path.isfile(rclone):
        print("SKIP the rclone sandbox checks (needs the pinned rclone: scripts/fetch-artifacts.sh rclone)")
        return
    rec = s3_fixture("sbx")
    state = storage.s3_dir("sbx")
    os.makedirs(state, exist_ok=True)
    open(os.path.join(state, "in.txt"), "w").write("a file of the volume\n")
    outside = tempfile.mkdtemp(prefix="mcst-sb-", dir="/private/var/tmp")
    private = os.path.join(TMP, "private-sbx")
    os.makedirs(private)
    open(os.path.join(private, "secret.txt"), "w").write("PRIVATE-MAC-FILE\n")
    try:
        plain = subprocess.run([rclone, "--config", "/dev/null", "copyto", os.path.join(state, "in.txt"),
                                os.path.join(outside, "plain.txt")], capture_output=True, text=True)
        check("(unconfined, rclone can write there: this user may)", os.path.exists(os.path.join(outside, "plain.txt")),
              plain.stderr[-200:])
        kw = {"state": state}
        rc, out = storage.run_rclone(["copyto", os.path.join(state, "in.txt"), os.path.join(outside, "out.txt")],
                                     rec["s3"], ("k", "s"), rclone, timeout=60, **kw)
        check("confined, rclone can't write outside the volume's state folder, even where this user may",
              not os.path.exists(os.path.join(outside, "out.txt")), (rc, out[-300:]))
        rc, out = storage.run_rclone(["copyto", os.path.join(private, "secret.txt"), os.path.join(state, "got.txt")],
                                     rec["s3"], ("k", "s"), rclone, timeout=60, **kw)
        check("...nor read a private file", not os.path.exists(os.path.join(state, "got.txt")) and "PRIVATE" not in out,
              (rc, out[-300:]))
        rc, out = storage.run_rclone(["copyto", os.path.join(state, "in.txt"), os.path.join(state, "sync", "in.txt")],
                                     rec["s3"], ("k", "s"), rclone, timeout=60, **kw)
        check("...and it still works inside it (where the sync copy is)", rc == 0 and os.path.exists(
            os.path.join(state, "sync", "in.txt")), (rc, out[-300:]))
    finally:
        shutil.rmtree(outside, ignore_errors=True)



# --- recheck 2 (round 3): each test fails against bd8cc46 ------------------------------------------
def test_image_paths():
    """Recheck 2 N1 (P1), Codex's five steps: a default destination (STORAGE_DIR, no --dir) gets the same checks
    and reservation as --dir; an image path is walked without following any link and bound to the file made."""
    pid = os.getpid()
    base = os.path.join(TMP, "n1")
    P = os.path.join(base, "P.noindex")
    private = os.path.join(base, "lume-another-vm")
    src = os.path.join(base, "refsrc")
    for d in (P, private, src):
        os.makedirs(d)
    open(os.path.join(src, "f.txt"), "w").write("x\n")
    saved = os.environ["MCOMP_STORAGE_DIR"]
    # 1. VM A holds P.noindex read-write
    storage.acquire([synth("folder", "pshare", P, True)], "vm-a", "/lab", pid)
    try:
        os.environ["MCOMP_STORAGE_DIR"] = P
        # 2. another configuration sets STORAGE_DIR=P.noindex and creates a volume without --dir
        e = raises(storage.cmd_volume_create, Args(name="n1disk", size="16M", dir=None, folder=None, s3=False))
        check("step 2: with STORAGE_DIR inside a folder a VM has read-write, creating a volume (no --dir) is refused",
              e is not None and not os.path.exists(os.path.join(P, "volumes", "n1disk.img")), e)
        e = raises(storage.cmd_refdisk_create, Args(name="n1ref", src=src, dir=None))
        check("...and so is a reference disk there", e is not None and not os.path.exists(os.path.join(P, "refdisks",
                                                                                                        "n1ref.iso")), e)
        os.environ["MCOMP_STORAGE_DIR"] = saved
        e = raises(storage.cmd_volume_create, Args(name="n1disk", size="16M", dir=P, folder=None, s3=False))
        check("...and --dir there", e is not None, e)
    finally:
        os.environ["MCOMP_STORAGE_DIR"] = saved
        storage.release_locks("vm-a", "/lab")
    os.environ["MCOMP_STORAGE_DIR"] = P
    try:
        e = raises(storage.cmd_volume_create, Args(name="n1disk", size="16M", dir=None, folder=None, s3=False))
    finally:
        os.environ["MCOMP_STORAGE_DIR"] = saved
    check("(with no VM holding it, the same default destination works)", e is None, e)
    check("...after which P.noindex can't be shared at all (it holds an image)",
          raises(storage.check_shareable_id, P) is not None)
    rec = storage.reg_load("volume", "n1disk")
    vols = os.path.join(P, "volumes")
    with open(os.path.join(private, "n1disk.img"), "wb") as f:
        f.write(b"PRIVATE")
        f.truncate(16 << 20)
    # 3. the image's folder replaced by a link to another VM's folder holding an image of the same name
    os.rename(vols, vols + "-was")
    os.symlink(private, vols)
    os.environ["MCOMP_VOLUMES"] = "n1disk"
    try:
        items, problems, _ = storage.build_plan()     # 4.-5. the planner must not adopt the private image
        e = raises(storage.cmd_volume_delete, Args(name="n1disk", yes=True))
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
        os.unlink(vols)
        os.rename(vols + "-was", vols)
    check("steps 3-5: a folder in the image's path replaced by a link isn't followed: that start is refused",
          problems and not any(storage.inside(it.get("path", ""), storage.canon_or_abs(private)) for it in items),
          (items, problems))
    check("...and volume delete through it deletes nothing (the other VM's image stays)", e is not None
          and os.path.exists(os.path.join(private, "n1disk.img")) and storage.reg_load("volume", "n1disk"), e)
    img = rec["path"]
    os.rename(img, img + ".orig")
    with open(img, "wb") as f:
        f.truncate(16 << 20)
    os.environ["MCOMP_VOLUMES"] = "n1disk"
    try:
        items, problems, _ = storage.build_plan()
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
        os.unlink(img)
        os.rename(img + ".orig", img)
    check("the image replaced at its own path (another file) is refused: images are bound to the file made",
          problems and not items, (items, problems))
    os.environ["MCOMP_VOLUMES"] = "n1disk"
    try:
        items, problems, _ = storage.build_plan()
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
    check("(the image itself, as made, still plans)", items and items[0]["path"] == rec["path"], (items, problems))
    raises(storage.cmd_volume_delete, Args(name="n1disk", yes=True))


def test_s3_staging():
    """Recheck 2 N2: files bound for the shared copy are made and quarantined in a staging folder no VM sees, on
    the same disk, and renamed into place; the staging folder can't be shared."""
    rec, shared, sync = s3_stage("stg")
    seen = []
    real = storage.copy_from_fd

    def spy(sfd, dfd, name, st, *a, **k):
        seen.append(storage.fd_path(dfd))
        return real(sfd, dfd, name, st, *a, **k)
    storage.copy_from_fd = spy
    try:
        r, _ = sync_with(rec, 'case "$1" in bisync) mkdir "$2/sub"; echo b > "$2/sub/from-bucket.txt" ;; esac\n'
                              'exit 0\n')
    finally:
        storage.copy_from_fd = real
    into = [p for p in seen if storage.inside(p, storage.canon_or_abs(shared))]
    check("files bound for the shared copy are made outside it (never under a temporary name in it)", r["ok"] and seen
          and not into and os.path.exists(os.path.join(shared, "sub", "from-bucket.txt")), (seen, r.get("summary")))
    stage = storage.stage_folder(rec) if hasattr(storage, "stage_folder") else ""
    ok = bool(stage) and os.path.isdir(stage) and os.stat(stage).st_dev == os.stat(shared).st_dev \
        and stat_mode(stage) == 0o700 and raises(storage.check_shareable_id, stage) is not None
    check("the staging folder is beside the copy (same disk), private, and can't be shared", ok, stage)
    v = storage.get_xattr(os.path.join(shared, "sub"), storage.QUARANTINE)
    check("a folder made in the shared copy carries the quarantine attribute", v is not None and storage.QUAR_OK.match(v))


def stat_mode(p):
    import stat as st_
    return st_.S_IMODE(os.stat(p).st_mode)


def test_checkpoint_order():
    """Recheck 2 N3: a checkpoint operation reads its manifest under the checkpoint's lock and reserves exactly
    the volumes it names; a volume delete takes the locks of the checkpoints that hold it."""
    pid = os.getpid()
    a = make_volume("cka")
    b = make_volume("ckb")
    vm = storage.vm_name()
    man_path = storage.ckpt_manifest(vm, "race")
    try:
        os.environ["MCOMP_VOLUMES"] = "ckb"
        quiet(storage.cmd_checkpoint, Args(op="save", name="race"))
        man_b = open(man_path).read()                 # the manifest a concurrent save of [ckb] writes
        snap_b = storage.snapshot_path(b["path"], vm, "race")
        shutil.copy2(snap_b, snap_b + ".keep")
        os.environ["MCOMP_VOLUMES"] = "cka"
        quiet(storage.cmd_checkpoint, Args(op="save", name="race"))
        os.replace(snap_b + ".keep", snap_b)          # that save's snapshot of ckb, as it would be
        with open(b["path"], "r+b") as f:
            f.write(b"B-NOW!")
        storage.acquire([{"kind": "disk", "name": "ckb", "path": b["path"], "lock": storage.lock_key("disk", b["path"])}],
                        "vm-b", "/lab", pid)
        real = storage.acquire
        fired = []

        def racing(items, holder, lab, p):
            if holder.startswith("operation:restore") and not fired:
                fired.append(1)
                open(man_path, "w").write(man_b)      # the save lands between the read and the locks
            return real(items, holder, lab, p)
        storage.acquire = racing
        try:
            e = raises(storage.cmd_checkpoint, Args(op="restore", name="race"))
        finally:
            storage.acquire = real
            storage.release_locks("vm-b", "/lab")
        check("a restore never rolls back a volume it didn't reserve (the manifest changed under it; a VM holds that "
              "volume)", open(b["path"], "rb").read(6) == b"B-NOW!", e)
        c = make_volume("ckc")
        os.environ["MCOMP_VOLUMES"] = "ckc"
        quiet(storage.cmd_checkpoint, Args(op="save", name="race2"))
        with storage.reserved([], "other", [storage.ckpt_item(vm, "race2")]):
            e = raises(storage.cmd_volume_delete, Args(name="ckc", yes=True))
        check("a volume delete takes its turn with the checkpoints that hold it (refused while one is busy)",
              e is not None and storage.reg_load("volume", "ckc"), e)
        raises(storage.cmd_volume_delete, Args(name="ckc", yes=True))
        check("(and goes ahead once it's free)", not storage.reg_load("volume", "ckc"))
    finally:
        os.environ["MCOMP_VOLUMES"] = ""


def test_space_pools():
    """Recheck 2 N4: APFS volumes in one container share one claim; a long copy re-checks free space."""
    a, b = TMP, "/System/Volumes/VM"          # the Data volume and the VM volume: one container, two devices

    def container(p):
        import plistlib
        dev = subprocess.run(["df", p], capture_output=True, text=True).stdout.splitlines()[-1].split()[0]
        out = subprocess.run(["diskutil", "info", "-plist", dev], capture_output=True).stdout
        return plistlib.loads(out).get("APFSContainerReference") if out else None
    if os.stat(a).st_dev == os.stat(b).st_dev or not container(a) or container(a) != container(b):
        print("SKIP the APFS container check (the two folders aren't separate volumes of one container here)")
    else:
        files = []
        try:
            with storage.space_claims([a, b], "pooltest") as g:
                files = [f for f in os.listdir(storage.space_dir()) if f.startswith("pooltest-")]
        except StorageErrorOrNone:
            g = {}
        check("two APFS volumes of one container (different device numbers) share one claim, not two",
              len(files) == 1 and g.get(a) == g.get(b), (files, g))
    src = os.path.join(TMP, "big.bin")
    with open(src, "wb") as f:
        for _ in range(13):
            f.write(os.urandom(1 << 20) * 10)
    out = os.path.join(TMP, "big-out")
    os.makedirs(out)
    calls = [0]
    real = storage.free_bytes_at

    def falling(fd):
        calls[0] += 1
        return (1 << 40) if calls[0] == 1 else (1 << 20)
    sfd = os.open(src, storage.FILE_NOFOLLOW)
    dfd = os.open(out, storage.DIR_NOFOLLOW)
    storage.free_bytes_at = falling
    try:
        e = raises(storage.copy_from_fd, sfd, dfd, "big-copy.bin", os.fstat(sfd), None, False)
    finally:
        storage.free_bytes_at = real
        os.close(sfd)
        os.close(dfd)
    check("a long copy re-checks the disk's free space as it goes and stops below the reserve",
          e is not None and not os.path.exists(os.path.join(out, "big-copy.bin")), (e, calls[0]))
    os.unlink(src)


StorageErrorOrNone = storage.StorageError


def test_s3_same_size_time():
    """Recheck 2 N5: both copies changed to different contents with the same size and time: compared, kept both."""
    rec, shared, sync = s3_stage("smt")
    open(os.path.join(shared, "same.txt"), "w").write("base\n")
    r, _ = sync_with(rec, "exit 0\n")
    open(os.path.join(storage.s3_dir("smt"), "bisync", "x.path1.lst"), "w").close()
    t = time.time() - 100
    for p, text in ((os.path.join(shared, "same.txt"), "AAAA\n"), (os.path.join(sync, "same.txt"), "BBBB\n")):
        open(p, "w").write(text)
        os.utime(p, (t, t))
    r, _ = sync_with(rec, "exit 0\n", resync=False)
    a_, b_ = open(os.path.join(shared, "same.txt")).read(), open(os.path.join(sync, "same.txt")).read()
    kept = os.path.exists(os.path.join(sync, "same.conflict1.txt")) or os.path.exists(os.path.join(shared,
                                                                                                    "same.conflict1.txt"))
    check("same size and time but different contents: a conflict, both kept (not recorded as in step)",
          "same.txt" in r.get("conflicts", []) and kept and a_ == b_, (r.get("conflicts"), a_, b_))
    t2 = time.time() - 50
    for p in (os.path.join(shared, "same.txt"), os.path.join(sync, "same.txt")):
        open(p, "w").write("CCCC\n")
        os.utime(p, (t2, t2))
    r, _ = sync_with(rec, "exit 0\n", resync=False)
    check("(the same contents with the same size and time are recorded as in step, no conflict)",
          r["ok"] and "same.txt" not in r.get("conflicts", []), r.get("conflicts"))


def test_s3_copy_destination():
    """Recheck 2 N6: an S3 local copy may go inside a registered folder (it's meant to be shared), not inside one a
    VM has read-write now."""
    fv = os.path.join(TMP, "n6", "folder.noindex")
    os.makedirs(fv)
    quiet(storage.cmd_volume_create, Args(name="n6fv", folder=fv, size=None, dir=None))
    args = dict(s3=True, bucket="my-bucket", endpoint="http://127.0.0.1:9", region=None, prefix=None, dir=fv, size=None,
                folder=None)
    e = raises(storage.cmd_volume_create, Args(name="n6s3", **args))
    check("an S3 local copy may go inside a registered folder volume no VM has read-write", e is None
          and storage.reg_load("volume", "n6s3"), e)
    storage.acquire([synth("folder", "n6fv", fv, True)], "vm-n6", "/lab", os.getpid())
    try:
        e = raises(storage.cmd_volume_create, Args(name="n6s3b", **args))
    finally:
        storage.release_locks("vm-n6", "/lab")
    check("...but not inside one a VM has read-write right now", e is not None and not storage.reg_load("volume", "n6s3b"),
          e)


def run_main(*args):
    try:
        return quiet(storage.main, list(args))
    except SystemExit as x:
        return x.code


def kernel_path(p):
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return storage.fd_path(fd)
    finally:
        os.close(fd)


def test_registered_spellings():
    """Recheck 3 P1 and #4: there's no adopting an item in place; one registered without its identity, or under
    another spelling of its path (a firmlink alias of the same folder), is refused with what to do (and what
    happens to the data); every registration is the kernel's own spelling."""
    check("there's no `volume adopt` (nothing is re-bound in place: an unknown command)",
          run_main("volume", "adopt", "x") == 2)
    check("...nor `refdisk adopt`", run_main("refdisk", "adopt", "x") == 2)
    rec = make_volume("spl")
    check("a new volume is registered under the kernel's own spelling of its image's path",
          rec["path"] == kernel_path(rec["path"]), rec["path"])
    alias = "/System/Volumes/Data" + rec["path"]
    if not os.path.exists(alias) or os.stat(alias).st_ino != os.stat(rec["path"]).st_ino:
        print("SKIP the firmlink alias check (no /System/Volumes/Data spelling of the test folder here)")
    else:
        storage.reg_save("volume", dict(rec, path=alias))
        os.environ["MCOMP_VOLUMES"] = "spl"
        try:
            items, problems, _ = storage.build_plan()
        finally:
            os.environ["MCOMP_VOLUMES"] = ""
            storage.reg_save("volume", rec)
        check("a volume recorded under a firmlink alias of its path (/System/Volumes/Data/...) is refused, not used "
              "(overlap checks compare one spelling)", problems and not any(it.get("path") == alias for it in items),
              (items, problems))
    storage.reg_save("volume", {k: v for k, v in rec.items() if k != "ident"})
    os.environ["MCOMP_VOLUMES"] = "spl"
    try:
        problems = storage.build_plan()[1]
    finally:
        os.environ["MCOMP_VOLUMES"] = ""
        storage.reg_save("volume", rec)
    msg = " ".join(problems)
    check("a volume registered without its identity is refused: delete and create again, saying what happens to the "
          "data", "volume delete spl" in msg and "copy out" in msg and "--keep-image" in msg and "adopt" not in msg, msg)
    rc = quiet(storage.cmd_volume_delete, Args(name="spl", yes=True, keep_image=True))
    check("volume delete --keep-image unregisters it and leaves the image file where it was",
          rc == 0 and not storage.reg_load("volume", "spl") and os.path.exists(rec["path"]))
    s3 = s3_fixture("spls3")
    storage.reg_save("volume", {k: v for k, v in s3.items() if k != "ident"})
    r, _ = sync_with(storage.reg_load("volume", "spls3"), "exit 0\n")
    check("an S3 volume without its identity isn't synced: delete and create again (keys and sync state go, the bucket "
          "and the local copy stay)", not r["ok"] and "volume delete spls3" in r.get("summary", "")
          and "local copy" in r.get("summary", "") and "adopt" not in r.get("summary", ""), r.get("summary"))


def test_stage_exclusion():
    """Recheck 3 #2: a staging folder may be seen by no VM: an S3 volume isn't made where a VM already sees the folder
    its staging folder goes in, and a sync is refused while any VM has a folder holding it, or it."""
    pid = os.getpid()
    parent = os.path.join(TMP, "stx", "parent")
    os.makedirs(parent)
    args = dict(s3=True, bucket="my-bucket", endpoint="http://127.0.0.1:9", region=None, prefix="team", dir=parent,
                size=None, folder=None)
    storage.acquire([synth("folder", "stxp", parent, False)], "vm-ro", "/lab", pid)
    try:
        e = raises(storage.cmd_volume_create, Args(name="stx", **args))
    finally:
        storage.release_locks("vm-ro", "/lab")
    check("an S3 volume isn't made where a VM already has the folder its staging folder would go in (read-only too)",
          e is not None and not storage.reg_load("volume", "stx")
          and not os.path.exists(os.path.join(parent, ".stx.mcstage.noindex")), e)
    e = raises(storage.cmd_volume_create, Args(name="stx", **args))
    check("(with no VM there, it's made, its staging folder with it)", e is None, e)
    rec = storage.reg_load("volume", "stx")
    storage.acquire([synth("folder", "stxp2", parent, False)], "vm-ro2", "/lab", pid)
    try:
        r, _ = sync_with(rec, "exit 0\n")
    finally:
        storage.release_locks("vm-ro2", "/lab")
    check("a sync is refused while a VM has a folder holding the staging folder (attached before; read-only too)",
          not r["ok"], r.get("summary"))
    stage = os.path.join(parent, ".stx.mcstage.noindex")
    storage.acquire([synth("folder", "stxs", stage, True)], "vm-rw", "/lab", pid)
    try:
        r, _ = sync_with(rec, "exit 0\n")
    finally:
        storage.release_locks("vm-rw", "/lab")
    check("...and while a VM has the staging folder itself", not r["ok"], r.get("summary"))
    r, _ = sync_with(rec, "exit 0\n")
    check("(with neither, it syncs)", r["ok"], r.get("summary"))


def test_stage_cleanup():
    """Recheck 3 #3: cleanup removes only what this program made (a recorded session's names), never anything else in
    the staging folder or the shared copy; a staging folder that isn't the one made is refused, untouched."""
    rec, shared, sync = s3_stage("scl")
    r, _ = sync_with(rec, "exit 0\n")
    open(os.path.join(storage.s3_dir("scl"), "bisync", "x.path1.lst"), "w").close()
    stage = os.path.join(os.path.dirname(shared), ".scl.mcstage.noindex")
    check("(the staging folder is there after a sync)", os.path.isdir(stage))
    keep, unrelated = os.path.join(stage, "keep.txt"), os.path.join(stage, ".metacomputer-tmp-unrelated")
    vmfile = os.path.join(shared, ".metacomputer-tmp-vm-notes")
    session = "0123456789abcdef"
    leftover = os.path.join(stage, f".metacomputer-tmp-{session}-x")
    for p in (keep, unrelated, vmfile, leftover):
        open(p, "w").write("x\n")
    storage.atomic_json(os.path.join(storage.s3_dir("scl"), "stage-sessions.json"), {"sessions": [session]})
    r, _ = sync_with(rec, "exit 0\n", resync=False)
    check("a sync removes from the staging folder only what an interrupted sync of its own left (a recorded session)",
          not os.path.exists(leftover) and os.path.exists(keep) and os.path.exists(unrelated), r.get("summary"))
    check("...and never a file of the VM's in the shared copy, whatever its name", os.path.exists(vmfile))
    os.rename(stage, stage + "-was")
    os.mkdir(stage, 0o700)
    precious = os.path.join(stage, "precious.txt")
    open(precious, "w").write("x\n")
    try:
        r, _ = sync_with(rec, "exit 0\n", resync=False)
        kept = os.path.exists(precious)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        os.rename(stage + "-was", stage)
    check("a staging folder replaced by another folder is refused, and nothing in it is touched",
          not r["ok"] and kept, r.get("summary"))


def test_status_untrusted_guest():
    """What the VM reports for storage (guest-status) can't break `status`: a non-object, or items that aren't a
    list of objects, are ignored (the host's own view is shown)."""
    host = {"configured": True, "guest_features": True, "items": [
        {"kind": "folder", "name": "f", "mount": "/mnt/f", "rw": False, "path": "/x"}]}
    bad = []
    for guest in ("junk", [1, 2], {"items": "x"}, {"items": [1, "a", None]}, {"supported": True, "items": [[]]}, 7,
                  {"supported": True, "items": [{"kind": [], "name": "x"}]},
                  {"supported": True, "items": [{"kind": "folder", "name": {"a": 1}, "mounted": True}]}):
        try:
            storage.describe_status(host, guest, True)
        except Exception as e:  # noqa: BLE001
            bad.append((guest, repr(e)))
    check("odd storage reports from the VM never break `status`", not bad, bad)


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
        test_s3()
        test_webdav_standin()
        test_s3_sandbox()
        test_s3_quarantine_failed_run()
        test_s3_space()
        test_s3_no_auto_resync()
        test_s3_prepare_overflow()
        test_overlap_order()
        test_reservation_lifetimes()
        test_link_rules()
        test_quoted_keys()
        test_copy_bounds()
        test_watch_bounds()
        test_fail_closed()
        test_host_side_stop()
        test_s3_two_stages()
        test_s3_exchange()
        test_s3_space_claims()
        test_rclone_planted()
        test_s3_real_rclone()
        test_image_paths()
        test_s3_staging()
        test_checkpoint_order()
        test_space_pools()
        test_s3_same_size_time()
        test_s3_copy_destination()
        test_registered_spellings()
        test_stage_exclusion()
        test_stage_cleanup()
        test_status_untrusted_guest()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
