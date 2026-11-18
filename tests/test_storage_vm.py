#!/usr/bin/env python3
"""Shared and persistent storage, end to end on a test VM (docs/storage.md).

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_storage_vm.py [V] [R] [P] [F] [S]

Use a test VM built from this branch (storage needs its guest side): this stops, starts and
resets it, and leaves it stopped. Volumes, reference disks and their registry live in a
temporary folder (MCOMP_STORAGE_HOME, STORAGE_DIR), removed at the end; variants of the given
config are written there too.

V  Disk volumes: created on the Mac, formatted in the VM on first use, written, kept across
   `reset`; a custom mount point (/volume); `start --no-volumes` attaches nothing; another VM
   is refused the volume while this one holds it; a missing image refuses the start; a VM
   without storage support (no guest-features) is refused before it boots; `checkpoint
   --with-volumes` and `restore` roll the volume back; `volume resize` grows it.
R  Reference disks: packed from a Mac folder (long and non-ASCII names, the exec bit kept, a
   link left out), two attached (one by --mount, one by --usb-storage), read-only in the VM.
P  put: every file checked by SHA-256, a taken name lands as "name (1).ext", --overwrite
   replaces, a folder lands whole; into a volume.
F  Folder volumes: read-write without .noindex refuses the start; a read-only folder (at
   /mnt/shared, as Cua Spaces scripts expect) rejects writes from the VM; in a read-write one,
   a file the VM writes gets the quarantine attribute on the Mac, a removed attribute comes
   back, a link out of the folder becomes a note (and a security event) while one inside stays,
   a FIFO goes; another VM is refused the folder read-write; Spotlight doesn't index what the
   VM writes (a .noindex folder in your home, next to a plain one that it does index; removed
   at the end); stop reports what the watcher did.
S  S3 volumes, against `rclone serve s3` on 127.0.0.1 (a random port, throwaway keys, a temporary
   folder; stopped at the end): keys stored through `volume keys` at security's own prompt (in a
   pseudo-terminal), `volume test`, a file another computer put in the bucket reaches the VM at
   start, a file the VM writes reaches the bucket on the interval, a file changed on both sides
   keeps both copies (and a task.s3_conflict event), a write just before stop reaches the bucket
   at stop; the keys appear in no process's arguments during a sync, in no file of the registry,
   logs or events, not in rclone.conf and nowhere in the VM; `volume delete` removes them from
   the Keychain. Only Keychain items this test made are touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(LAB, "bin", "metacomputer")
BASE = os.environ.get("MCOMP_CONFIG") or ""
TMP = tempfile.mkdtemp(prefix="mcst-vm-", dir="/private/tmp")
STATE = os.path.join(TMP, "state")
DATA = os.path.join(TMP, "data")
results = []
sys.path.insert(0, os.path.join(LAB, "lib"))
from labconfig import GUEST_HOME as GH  # noqa: E402


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)


def variant(name, *lines):
    p = os.path.join(TMP, name + ".env")
    with open(p, "w") as f:
        f.write(f". {BASE}\nSTORAGE_DIR={DATA}\n" + "\n".join(lines) + "\n")
    return p


def mc(cfg, *args, stdin=None, timeout=600):
    env = dict(os.environ, MCOMP_CONFIG=cfg, MCOMP_STORAGE_HOME=STATE)
    return subprocess.run([BIN, *args], input=stdin, capture_output=True, text=True, timeout=timeout, env=env)


def sh(cfg, script, timeout=120):
    r = mc(cfg, "sh", "--override", "-c", script, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def out(r):
    return (r.stdout + r.stderr).strip()


def stop(cfg):
    return mc(cfg, "stop", "--override", timeout=300)


def start(cfg, *extra):
    return mc(cfg, "start", *extra, timeout=600)


def test_v():
    print("V  disk volumes")
    plain = variant("plain")
    stop(plain)
    r = mc(plain, "volume", "create", "work", "--size", "64M")
    check("volume create makes a sparse image in STORAGE_DIR", r.returncode == 0
          and os.path.isfile(os.path.join(DATA, "volumes", "work.img")), out(r))
    mc(plain, "volume", "create", "cache", "--size", "32M")
    cfg = variant("v1", 'VOLUMES="work,cache:/volume"')
    r = start(cfg)
    check("start attaches both: formatted on first use", r.returncode == 0 and out(r).count("formatted (first use)") == 2,
          out(r))
    rc, so, se = sh(cfg, "findmnt -n -o FSTYPE,OPTIONS ~/data/work; findmnt -n -o FSTYPE /volume; "
                         "echo kept > ~/data/work/kept.txt && echo vol > /volume/v.txt && sync && stat -c %U ~/data/work")
    check("in the VM: ext4 at ~/data/work (nodev,nosuid) and at /volume, writable by the desktop user",
          rc == 0 and "ext4" in so and "nosuid" in so and "nodev" in so and so.strip().endswith("cua"), so + se)
    r = mc(cfg, "status")
    check("status lists both, mounted", "work" in r.stdout and "/volume" in r.stdout and r.stdout.count("✓") >= 2, r.stdout[-600:])
    other = variant("other", "VM_NAME=mcst-other-vm", f"RUN_DIR={TMP}/other-run", 'VOLUMES="work"')
    os.makedirs(os.path.join(TMP, "other-run"), exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "storage.py"), "prepare", "--pid", str(os.getpid())],
                       capture_output=True, text=True, env=dict(os.environ, MCOMP_STORAGE_HOME=STATE, MCOMP_STORAGE_DIR=DATA,
                       MCOMP_VM_NAME="mcst-other-vm", MCOMP_VOLUMES="work", MCOMP_REFDISKS="",
                       MCOMP_RUN_DIR=os.path.join(TMP, "other-run"), MCOMP_SSH_DIR=settings(cfg)["ssh_dir"]))
    check("another VM is refused the volume while this one runs, naming the holder",
          r.returncode != 0 and "in use by VM" in r.stderr, r.stderr)
    r = mc(cfg, "reset", "--yes", "--override")
    rc, so, _ = sh(cfg, "cat ~/data/work/kept.txt /volume/v.txt")
    check("after reset: the volume's files are there (not formatted again)", r.returncode == 0 and so.split() == ["kept", "vol"]
          and "formatted" not in out(r), out(r)[-400:] + so)
    stop(cfg)
    r = start(cfg, "--no-volumes")
    rc, so, _ = sh(cfg, "findmnt -n ~/data/work /volume | wc -l")
    check("start --no-volumes attaches nothing", r.returncode == 0 and so.strip() == "0", out(r)[-300:] + so)
    stop(cfg)
    img = os.path.join(DATA, "volumes", "work.img")
    os.rename(img, img + ".away")
    r = start(cfg)
    check("a missing image refuses the start, saying where and how to start without it",
          r.returncode != 0 and "missing" in out(r) and "--no-volumes" in out(r), out(r))
    os.rename(img + ".away", img)
    nofeat = variant("nofeat", f"SSH_DIR={TMP}/no-features", 'VOLUMES="work"')
    os.makedirs(os.path.join(TMP, "no-features"), exist_ok=True)
    r = start(nofeat)
    check("a VM built before storage support is refused before it boots (rebuild, or --no-volumes)",
          r.returncode != 0 and "build --replace" in out(r) and "--no-volumes" in out(r) and "VM running" not in out(r), out(r))
    r = mc(cfg, "checkpoint", "mcst-c1", "--with-volumes")
    check("checkpoint --with-volumes snapshots the volumes", r.returncode == 0 and "with its volumes" in out(r), out(r))
    r = mc(cfg, "checkpoints")
    check("checkpoints lists the volumes it holds", "mcst-c1" in r.stdout and "work" in r.stdout and "cache" in r.stdout, r.stdout)
    start(cfg)
    sh(cfg, "echo changed > ~/data/work/kept.txt; rm /volume/v.txt; sync")
    stop(cfg)
    r = mc(cfg, "restore", "mcst-c1", "--yes", "--override")
    rc, so, _ = sh(cfg, "cat ~/data/work/kept.txt /volume/v.txt")
    check("restore rolls the volumes back with the VM", r.returncode == 0 and so.split() == ["kept", "vol"], out(r)[-400:] + so)
    stop(cfg)
    r = mc(cfg, "volume", "resize", "work", "--size", "128M")
    r2 = start(cfg)
    rc, so, _ = sh(cfg, "df -B1M --output=size ~/data/work | tail -1; cat ~/data/work/kept.txt")
    check("volume resize grows it; the filesystem grows at the next start, files kept",
          r.returncode == 0 and "grown" in out(r2) and int(so.split()[0]) > 100 and so.split()[1] == "kept", out(r2)[-300:] + so)
    stop(cfg)
    r = mc(cfg, "checkpoint", "--delete", "mcst-c1", "--yes")
    check("checkpoint --delete removes the volume snapshots too", r.returncode == 0 and not any(
        f.startswith(".work.img.ckpt") for f in os.listdir(os.path.join(DATA, "volumes"))), out(r))
    r = mc(plain, "volume", "list", "--json")
    vols = {v["name"]: v for v in json.loads(r.stdout)["volumes"]}
    check("volume list: both free after stop, with sizes", vols["work"]["state"] == "free" and vols["work"]["size"] == 128 << 20,
          vols)


def settings(cfg):
    o = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s\\n" "$SSH_DIR" "$VM_NAME"'],
                       capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfg)).stdout.split("\n")
    return {"ssh_dir": o[0], "vm": o[1]}


def test_r():
    print("R  reference disks")
    plain = variant("plain")
    src = os.path.join(TMP, "refsrc")
    long = "n" * 150 + ".txt"
    os.makedirs(os.path.join(src, "sub"))
    open(os.path.join(src, long), "w").write("long\n")
    open(os.path.join(src, "café über.txt"), "w").write("unicode\n")
    with open(os.path.join(src, "run.sh"), "w") as f:
        f.write("#!/bin/sh\necho ran\n")
    os.chmod(os.path.join(src, "run.sh"), 0o755)
    open(os.path.join(src, "sub", "inner.md"), "w").write("inner\n")
    os.symlink("/etc/hosts", os.path.join(src, "escape"))
    r = mc(plain, "refdisk", "create", "docs", "--from", src)
    check("refdisk create packs the folder (read-only image), leaving the link out", r.returncode == 0
          and "left out 1" in out(r) and not os.access(os.path.join(DATA, "refdisks", "docs.iso"), os.W_OK), out(r))
    mc(plain, "refdisk", "create", "docs2", "--from", os.path.join(src, "sub"))
    r = mc(plain, "refdisk", "create", "home", "--from", os.path.expanduser("~"))
    check("refdisk create refuses the home folder", r.returncode != 0 and "private" in out(r), out(r))
    cfg = variant("r1", 'REFDISKS="docs:/mnt/docs,docs2"')
    stop(cfg)
    r = start(cfg)
    rc, so, se = sh(cfg, f"cat /mnt/docs/{long} '/mnt/docs/café über.txt' ~/ref/docs2/inner.md; /mnt/docs/run.sh; "
                         "ls /mnt/docs; touch /mnt/docs/new 2>&1; findmnt -n -o OPTIONS /mnt/docs")
    check("both reference disks attach (one --mount, one USB)", r.returncode == 0 and out(r).count("refdisk") == 2, out(r))
    check("long and non-ASCII names, contents and the exec bit come through", "long" in so and "unicode" in so
          and "inner" in so and "ran" in so, so + se)
    check("the link was left out", "escape" not in so, so)
    check("read-only in the VM", "Read-only file system" in so and "ro," in so, so)
    stop(cfg)


def test_p():
    print("P  put")
    cfg = variant("p1", 'VOLUMES="work"')
    stop(cfg)
    start(cfg)
    d = os.path.join(TMP, "putsrc")
    os.makedirs(os.path.join(d, "folder one", "sub"))
    open(os.path.join(d, "report.pdf"), "w").write("v1\n")
    open(os.path.join(d, "folder one", "a.txt"), "w").write("a\n")
    open(os.path.join(d, "folder one", "sub", "b.bin"), "wb").write(os.urandom(300000))
    sh(cfg, "rm -rf ~/Inbox/mcst-put; mkdir -p ~/Inbox/mcst-put")
    r = mc(cfg, "put", os.path.join(d, "report.pdf"), os.path.join(d, "folder one"), "--to", "~/Inbox/mcst-put")
    check("put copies a file and a folder, every file verified", r.returncode == 0 and "3 files verified (SHA-256)" in r.stderr
          and r.stdout.split("\n")[0].endswith("/report.pdf"), out(r))
    r = mc(cfg, "put", os.path.join(d, "report.pdf"), os.path.join(d, "folder one"), "--to", "~/Inbox/mcst-put")
    check("the same names again land as 'report (1).pdf' and 'folder one (1)'", r.returncode == 0
          and r.stdout.split("\n")[:2] == [f"{GH}/Inbox/mcst-put/report (1).pdf", f"{GH}/Inbox/mcst-put/folder one (1)"],
          out(r))
    open(os.path.join(d, "report.pdf"), "w").write("v2\n")
    r = mc(cfg, "put", os.path.join(d, "report.pdf"), "--to", "~/Inbox/mcst-put", "--overwrite")
    rc, so, _ = sh(cfg, "cat ~/Inbox/mcst-put/report.pdf; ls -A ~/Inbox/mcst-put; sha256sum '~/Inbox/mcst-put/folder one/sub/b.bin' "
                        "2>/dev/null; sha256sum ~/Inbox/mcst-put/folder\\ one/sub/b.bin")
    import hashlib
    want = hashlib.sha256(open(os.path.join(d, "folder one", "sub", "b.bin"), "rb").read()).hexdigest()
    check("--overwrite replaces; nothing else is left behind (no staging folder)", r.returncode == 0 and so.startswith("v2")
          and ".mcomp-put" not in so, so)
    check("the binary file arrived byte for byte", want in so, so)
    r = mc(cfg, "put", os.path.join(d, "report.pdf"), "--to", "~/data/work")
    check("put into a volume", r.returncode == 0 and r.stdout.strip() == f"{GH}/data/work/report.pdf", out(r))
    r = mc(cfg, "put", os.path.join(d, "nope.txt"))
    check("a missing source is refused", r.returncode != 0 and "no such file" in out(r), out(r))
    stop(cfg)


def test_f():
    print("F  folder volumes")
    plain = variant("plain")
    stop(plain)
    ro = os.path.join(TMP, "ro-share")
    os.makedirs(ro)
    open(os.path.join(ro, "from-mac.txt"), "w").write("mac\n")
    spot = tempfile.mkdtemp(prefix="mcst-spotlight-", dir=os.path.expanduser("~"))
    rw = os.path.join(spot, "rw-share.noindex")
    control = os.path.join(spot, "plain")
    os.makedirs(rw)
    os.makedirs(control)
    token = "mcsttok" + os.urandom(6).hex()
    open(os.path.join(control, token + "_control.txt"), "w").write(f"{token} control\n")
    try:
        mc(plain, "volume", "create", "fro", "--folder", ro)
        mc(plain, "volume", "create", "frw", "--folder", rw)
        mc(plain, "volume", "create", "fctl", "--folder", control)
        r = start(variant("f-bad", 'VOLUMES="fctl:rw"'))
        check("read-write without .noindex refuses the start (Spotlight)", r.returncode != 0 and ".noindex" in out(r), out(r))
        cfg = variant("f1", 'VOLUMES="fro:/mnt/shared,frw:/data/rw:rw"')
        r = start(cfg)
        check("start attaches a read-only and a read-write folder, and starts the watcher", r.returncode == 0
              and "folder 'fro' at /mnt/shared (ro)" in out(r) and "(rw)" in out(r) and "watcher running" in out(r), out(r))
        rc, so, se = sh(cfg, "cat /mnt/shared/from-mac.txt; touch /mnt/shared/x 2>&1; echo vm > /mnt/shared/from-mac.txt 2>&1; "
                             "findmnt -n -o OPTIONS /mnt/shared")
        check("the read-only folder: readable, every write refused", "mac" in so and (so + se).count("Read-only file system") == 2
              and so.strip().splitlines()[-1].startswith("ro"), so + se)
        check("...and the Mac's file is unchanged", open(os.path.join(ro, "from-mac.txt")).read() == "mac\n")
        rc, so, se = sh(cfg, f"cd /data/rw && echo vm > vm.txt && echo '{token} written by the VM' > {token}_vm.txt && "
                             "ln -s ~/.ssh esc-abs && ln -s ../../.. esc-rel && ln -s vm.txt inside && mkfifo pipe && "
                             "mkdir -p Tool.app/Contents && echo ok")
        time.sleep(6)
        q = subprocess.run(["xattr", "-p", "com.apple.quarantine", os.path.join(rw, "vm.txt")], capture_output=True, text=True)
        check("a file the VM wrote carries the quarantine attribute on the Mac", rc == 0 and q.stdout.strip().endswith(
            ";metacomputer;") and q.stdout.startswith("0081;"), q.stdout + q.stderr + so + se)
        q2 = subprocess.run(["xattr", "-p", "com.apple.quarantine", os.path.join(rw, "Tool.app")], capture_output=True, text=True)
        check("...and so does a folder it made (an app bundle)", q2.returncode == 0, q2.stderr)
        check("links out of the folder became notes", not os.path.lexists(os.path.join(rw, "esc-abs"))
              and not os.path.lexists(os.path.join(rw, "esc-rel")) and os.path.isfile(os.path.join(rw, "esc-abs.link-removed.txt")),
              os.listdir(rw))
        check("a link inside the folder stays; the FIFO is gone", os.path.islink(os.path.join(rw, "inside"))
              and not os.path.lexists(os.path.join(rw, "pipe")), os.listdir(rw))
        sh(cfg, "cd /data/rw && python3 -c 'import os; os.removexattr(\"vm.txt\", \"com.apple.quarantine\")'")
        time.sleep(6)
        q = subprocess.run(["xattr", "-p", "com.apple.quarantine", os.path.join(rw, "vm.txt")], capture_output=True, text=True)
        check("the VM removing the attribute: it's put back", q.returncode == 0 and ";metacomputer;" in q.stdout, q.stderr)
        logs = os.path.join(settings_logs(cfg), "events.jsonl")
        evs = [json.loads(l) for l in open(logs)] if os.path.exists(logs) else []
        types = {e.get("type") for e in evs if e.get("data", {}).get("volume") == "frw"}
        check("security events for the removed links and FIFO and the restored attribute (local event log)",
              {"symlink_neutralized", "special_file_removed", "quarantine_restored"} <= types, types)
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "storage.py"), "prepare", "--pid", str(os.getpid())],
                           capture_output=True, text=True, env=dict(os.environ, MCOMP_STORAGE_HOME=STATE, MCOMP_STORAGE_DIR=DATA,
                           MCOMP_VM_NAME="mcst-other-vm", MCOMP_VOLUMES="frw:rw", MCOMP_REFDISKS="",
                           MCOMP_RUN_DIR=os.path.join(TMP, "other-run"), MCOMP_SSH_DIR=settings(cfg)["ssh_dir"]))
        check("another VM is refused the folder read-write", r.returncode != 0 and "in use" in r.stderr, r.stderr)
        r = mc(cfg, "status")
        check("status shows both folders and the watcher", "/mnt/shared" in r.stdout and "watcher  running" in r.stdout,
              r.stdout[-700:])
        r = stop(cfg)
        check("stop reports what the watcher did", "link(s)" in out(r) and "quarantine restored" in out(r), out(r))
        found_ctl, found_vm = mdfind_wait(token, control, rw)
        if found_ctl:
            check("Spotlight: the plain folder's file is indexed, the VM's file in the .noindex folder isn't",
                  not found_vm, (found_ctl, found_vm))
        else:
            print("  SKIP Spotlight: it hadn't indexed even the plain folder within 3 minutes (is indexing on?)")
    finally:
        shutil.rmtree(spot, ignore_errors=True)


def test_s():
    print("S  S3 volumes")
    import pty
    import select
    import threading
    plain = variant("plain")
    stop(plain)
    rclone = os.path.join(LAB, ".cache")
    vers = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s" "$RCLONE_VERSION"'], capture_output=True,
                          text=True, env=dict(os.environ, MCOMP_CONFIG=plain)).stdout
    rclone = os.path.join(LAB, ".cache", f"rclone-v{vers}", "rclone")
    if not os.path.isfile(rclone):
        subprocess.run([os.path.join(LAB, "scripts", "fetch-artifacts.sh"), "rclone"], check=True, capture_output=True)
    served = os.path.join(TMP, "served")
    os.makedirs(os.path.join(served, "mcstbucket"))
    kid, ksec = "mcstkey" + os.urandom(8).hex(), "mcstsecret" + os.urandom(16).hex()
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    # The stand-in takes its keys from its environment too (a CSV value: the pair is one field), so the only
    # process that could show a key on its command line is one of ours.
    srv = subprocess.Popen([rclone, "serve", "s3", "--addr", f"127.0.0.1:{port}", "--config", "/dev/null",
                            "--dir-cache-time", "1s", served], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"),
                                "RCLONE_AUTH_KEY": f'"{kid},{ksec}"'})
    name = "mcst-s3-" + os.urandom(3).hex()
    other_env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "RCLONE_CONFIG": "/dev/null",
                 "RCLONE_CONFIG_OTHER_TYPE": "s3", "RCLONE_CONFIG_OTHER_PROVIDER": "Other",
                 "RCLONE_CONFIG_OTHER_ENDPOINT": f"http://127.0.0.1:{port}", "RCLONE_CONFIG_OTHER_ACCESS_KEY_ID": kid,
                 "RCLONE_CONFIG_OTHER_SECRET_ACCESS_KEY": ksec}

    def other_put(rel, text):
        """Another computer with the bucket's keys writes a file (through the S3 API)."""
        src = os.path.join(TMP, "other-" + rel.replace("/", "_"))
        open(src, "w").write(text)
        return subprocess.run([rclone, "copyto", src, f"other:mcstbucket/team/{rel}"], env=other_env,
                              capture_output=True).returncode == 0

    def bucket(rel):
        p = os.path.join(served, "mcstbucket", "team", rel)
        return open(p).read() if os.path.isfile(p) else None

    try:
        time.sleep(1.5)
        r = mc(plain, "volume", "create", name, "--s3", "--bucket", "mcstbucket", "--endpoint", f"http://127.0.0.1:{port}",
               "--prefix", "team")
        check("volume create --s3 makes a local copy (.noindex) and says to store the keys next", r.returncode == 0
              and "volume keys" in r.stdout, out(r))
        r = mc(plain, "volume", "keys", name)
        check("volume keys refuses without a terminal", r.returncode != 0 and "own terminal" in out(r), out(r))
        ok, transcript = keys_at_prompt(plain, name, kid, ksec)
        check("volume keys stores both keys through security's own prompt (typed, never on a command line)", ok,
              transcript[-400:])
        r = mc(plain, "volume", "test", name)
        check("volume test: the endpoint answers, the keys list and write; versioning reported", r.returncode == 0
              and out(r).count("✓") >= 4 and "versioning" in out(r), out(r))
        check("another computer puts a file in the bucket", other_put("from-bucket.txt", "hello from the bucket\n"))
        cfg = variant("s1", f'VOLUMES="{name}:/mnt/s3:rw"', "S3_SYNC_INTERVAL=15")
        r = start(cfg)
        check("start syncs the S3 volume first, then attaches its local copy read-write", r.returncode == 0
              and "first sync" in out(r) and "/mnt/s3 (rw)" in out(r), out(r))
        rc, so, se = sh(cfg, "cat /mnt/s3/from-bucket.txt")
        check("the bucket's file is in the VM", "hello from the bucket" in so, so + se)
        local = json.loads(mc(plain, "volume", "list", "--json").stdout)
        lpath = [v for v in local["volumes"] if v["name"] == name][0]["path"]
        q = subprocess.run(["xattr", "-p", "com.apple.quarantine", os.path.join(lpath, "from-bucket.txt")],
                           capture_output=True, text=True)
        check("on the Mac, what came from the bucket carries the quarantine attribute", ";metacomputer;" in q.stdout, q.stderr)
        sh(cfg, "echo written in the VM > /mnt/s3/from-vm.txt; echo vm-version > /mnt/s3/shared.txt")
        found = wait_for(lambda: bucket("from-vm.txt"), 60)
        check("a file the VM writes reaches the bucket on the interval sync", found and "written in the VM" in found, found)
        check("(both sides have shared.txt)", wait_for(lambda: bucket("shared.txt"), 40))
        sh(cfg, "echo vm-changed > /mnt/s3/shared.txt")
        time.sleep(2)
        other_put("shared.txt", "bucket-changed\n")
        conflicted = wait_for(lambda: bucket("shared.conflict1.txt") or bucket("shared.conflict2.txt"), 70)
        rc, so, se = sh(cfg, "cat /mnt/s3/shared.txt; ls /mnt/s3")
        check("a file changed on both sides keeps both copies (newer keeps the name, the other a .conflictN name), "
              "in the bucket and the VM", conflicted and "bucket-changed" in (bucket("shared.txt") or "")
              and "vm-changed" in conflicted and "shared.conflict" in so, (bucket("shared.txt"), conflicted, so))
        logs = os.path.join(settings_logs(cfg), "events.jsonl")
        evs = [json.loads(l) for l in open(logs)] if os.path.exists(logs) else []
        check("...and a task.s3_conflict event", any(e.get("type") == "s3_conflict" and e.get("data", {}).get("volume") == name
                                                     for e in evs))
        seen = []
        stop_poll = {"now": False}

        def poll():
            while not stop_poll["now"]:
                ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout
                if kid in ps or ksec in ps:
                    seen.append([l for l in ps.splitlines() if kid in l or ksec in l][:3])
                time.sleep(0.05)
        t = threading.Thread(target=poll, daemon=True)
        t.start()
        sh(cfg, "echo poll > /mnt/s3/poll.txt")
        r = mc(cfg, "volume", "sync", name)
        stop_poll["now"] = True
        t.join()
        check("during a sync, no process's command line holds a key", r.returncode == 0 and not seen, (out(r), seen))
        check("the next sync doesn't report the settled conflict again (and makes no new copy)",
              "conflict" not in out(r) and not bucket("shared.conflict2.txt"), out(r))
        # The script names each key in two halves, so the script file itself doesn't hold one.
        k1, k2, s1, s2 = kid[:6], kid[6:], ksec[:6], ksec[6:]
        rc, so, se = sh(cfg, f"a={k1}; b={k2}; c={s1}; d={s2}; "
                             'grep -rIl --exclude-dir="mcsh.*" -e "$a$b" -e "$c$d" /home /tmp /mnt /etc /opt 2>/dev/null; '
                             'env | grep -c -e "$a$b" -e "$c$d"')
        check("the keys are nowhere in the VM (files in /home, /tmp, /mnt, /etc, /opt; the environment)",
              so.strip() == "0", so + se)
        sh(cfg, "echo last words > /mnt/s3/last.txt")
        r = stop(cfg)
        check("stop syncs once more, after the VM is off: the last write reaches the bucket",
              "last words" in (bucket("last.txt") or "") and "synced with" in out(r), out(r)[-400:])
        leaks = []
        for root in (STATE, DATA, settings_logs(cfg)):
            for dd, _, fs in os.walk(root):
                for f in fs:
                    try:
                        text = open(os.path.join(dd, f), errors="replace").read()
                    except OSError:
                        continue
                    if kid in text or ksec in text:
                        leaks.append(os.path.join(dd, f))
        check("no file in the registry, the data folder or the logs holds a key", not leaks, leaks)
        conf = os.path.join(STATE, "s3", "rclone.conf")
        check("rclone.conf is empty", os.path.isfile(conf) and os.path.getsize(conf) == 0)
    finally:
        r = mc(plain, "volume", "delete", name, "--yes")
        gone = all(subprocess.run(["security", "delete-generic-password", "-s", f"metacomputer-s3:{name}", "-a", a],
                                  capture_output=True).returncode == 44 for a in ("access_key_id", "secret_access_key"))
        check("volume delete removes the keys from the Keychain", gone, out(r))
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()


def wait_for(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(2)
    return None


def keys_at_prompt(cfg, name, kid, ksec):
    """Run `volume keys` in a pseudo-terminal and answer security's prompts, as a person would."""
    import pty
    import select
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(MCOMP_CONFIG=cfg, MCOMP_STORAGE_HOME=STATE)
        os.execv(BIN, [BIN, "volume", "keys", name])
    buf, answers = "", [kid, kid, ksec, ksec]
    end = time.time() + 60
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 1)
        if fd in r:
            try:
                chunk = os.read(fd, 4096).decode(errors="replace")
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if answers and buf.rstrip().lower().endswith(("item:", "password:")):
                os.write(fd, (answers.pop(0) + "\n").encode())
                buf += "\n[answered]\n"
    _, status = os.waitpid(pid, 0)
    clean = buf.replace(kid, "[kid]").replace(ksec, "[secret]")
    return os.WEXITSTATUS(status) == 0 and not answers and "are in your login Keychain" in buf, clean


def mdfind_wait(token, control, rw, wait=180):
    end = time.time() + wait
    ctl = vm = False
    while time.time() < end:
        r = subprocess.run(["mdfind", "-name", token], capture_output=True, text=True)
        hits = r.stdout.split("\n")
        ctl = any(h.startswith(control) for h in hits)
        vm = any(h.startswith(rw) for h in hits)
        if ctl:
            time.sleep(10)
            r = subprocess.run(["mdfind", "-name", token], capture_output=True, text=True)
            vm = any(h.startswith(rw) for h in r.stdout.split("\n"))
            return ctl, vm
        time.sleep(10)
    return ctl, vm


def settings_logs(cfg):
    return subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s" "$LOG_DIR"'], capture_output=True,
                          text=True, env=dict(os.environ, MCOMP_CONFIG=cfg)).stdout


def main(argv):
    if not BASE or not os.path.isfile(BASE):
        print("set MCOMP_CONFIG to a test VM's config (this stops, starts and resets that VM)", file=sys.stderr)
        return 2
    which = [a.upper() for a in argv] or ["V", "R", "P", "F", "S"]
    os.makedirs(os.path.join(TMP, "other-run"), exist_ok=True)
    try:
        for k in which:
            {"V": test_v, "R": test_r, "P": test_p, "F": test_f, "S": test_s}[k]()
    finally:
        stop(variant("final"))
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
