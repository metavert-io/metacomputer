#!/usr/bin/env python3
"""Shared and persistent storage, end to end on a test VM (docs/storage.md).

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_storage_vm.py [V] [R] [P] [F]

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
    which = [a.upper() for a in argv] or ["V", "R", "P", "F"]
    os.makedirs(os.path.join(TMP, "other-run"), exist_ok=True)
    try:
        for k in which:
            {"V": test_v, "R": test_r, "P": test_p, "F": test_f}[k]()
    finally:
        stop(variant("final"))
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
