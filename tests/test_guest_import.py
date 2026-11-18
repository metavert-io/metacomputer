#!/usr/bin/env python3
"""Files copied from the VM to the Mac (lib/guest_import.py and its callers). No VM:
a fake `ssh` and a fake Lume stand in for a hostile VM that sends a tar stream with
links and special files. Needs ffmpeg on the Mac.

    python3 tests/test_guest_import.py

It checks that:
- only regular files and folders arrive; symbolic and hard links, FIFOs, devices,
  absolute names, `..` and names with control or bidi characters are left out and
  reported (escaped);
- a link the VM names `x-contact.png`, pointing at a Mac file, leaves that file
  untouched through `task finish` (which builds contact sheets) and `get`;
- a link already on the Mac where a file arrives is replaced, never written through;
  a folder entered through a link is refused;
- a stream cut off part-way keeps only the files that arrived whole;
- `contact-sheet` refuses an output that's a link and writes through a temporary
  file it then renames;
- `recordings --sync` goes through the same importer, and the listing escapes what
  it prints.
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import guest_import  # noqa: E402

CLI = os.path.join(LAB, "bin", "metacomputer")
IMPORT = os.path.join(LAB, "lib", "guest_import.py")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def add_file(tf, name, data, mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = 1700000000
    tf.addfile(ti, io.BytesIO(data))


def add_special(tf, name, kind, target=""):
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.linkname = target
    ti.mode = 0o644
    tf.addfile(ti)


def add_dir(tf, name):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    tf.addfile(ti)


def tiny_video(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "2",
                    "-pix_fmt", "yuv420p", path], check=True, stdin=subprocess.DEVNULL)
    return open(path, "rb").read()


def hostile_tar(video, victim, victim_dir, prefix="."):
    """What a hostile VM sends: a real video, a link named like its contact sheet pointing at a
    Mac file, and every other kind of entry that isn't a plain file or folder."""
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as tf:
        add_dir(tf, prefix)
        add_dir(tf, f"{prefix}/recording")
        add_file(tf, f"{prefix}/recording/x.mp4", video)
        add_special(tf, f"{prefix}/recording/x-contact.png", tarfile.SYMTYPE, victim)
        add_special(tf, f"{prefix}/pipe", tarfile.FIFOTYPE)
        add_special(tf, f"{prefix}/hard", tarfile.LNKTYPE, victim)
        add_special(tf, f"{prefix}/dev", tarfile.CHRTYPE)
        add_special(tf, f"{prefix}/escape", tarfile.SYMTYPE, victim_dir)
        add_file(tf, f"{prefix}/escape/owned.txt", b"written through a link\n")
        add_file(tf, "/abs-planted.txt", b"absolute\n")
        add_file(tf, f"{prefix}/../up-planted.txt", b"up\n")
        add_file(tf, f"{prefix}/evil\x1b]52;c;SGk=\x07name.txt", b"escape in the name\n")
        add_file(tf, f"{prefix}/new\nline.mp4", b"not a video\n")
        add_dir(tf, f"{prefix}/files")
        add_file(tf, f"{prefix}/files/result.txt", b"the real output\n", 0o755)
    return b.getvalue()


def links_or_special(root):
    bad = []
    for d, dirs, fs in os.walk(root):
        for n in dirs + fs:
            st = os.lstat(os.path.join(d, n))
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                bad.append(os.path.join(d, n))
    return bad


def fake_vm(td, payload):
    """A fake ssh (prints PAYLOAD for any tar command) and a fake Lume (the VM is running)."""
    bindir = os.path.join(td, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(bindir, "ssh"), "w") as f:
        f.write("#!/bin/sh\n"
                "for a in \"$@\"; do last=\"$a\"; done\n"
                "case \"$last\" in *tar*) cat \"$MCOMP_TEST_TAR\" ;; *) exit 0 ;; esac\n")
    with open(os.path.join(bindir, "lume"), "w") as f:
        f.write("#!/bin/sh\n"
                "case \"$1\" in get) echo '{\"status\": \"running\"}' ;; ls) echo '[]' ;; esac\n")
    for n in ("ssh", "lume"):
        os.chmod(os.path.join(bindir, n), 0o755)
    tarpath = os.path.join(td, "guest.tar")
    with open(tarpath, "wb") as f:
        f.write(payload)
    cfg = os.path.join(td, "test.env")
    with open(cfg, "w") as f:
        f.write(f'VM_NAME=fake-import-vm\nLUME="{bindir}/lume"\nSSH_DIR="{td}/ssh"\nSSH_PORT=1\n'
                f'RUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\nACTION_LOG=off\n')
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), MCOMP_CONFIG=cfg,
               MCOMP_TEST_TAR=tarpath, MCOMP_EVENTS_DIR=os.path.join(td, "events"))
    return env


def main():
    if not shutil.which("ffmpeg"):
        print("SKIP: no ffmpeg")
        return 0
    td = tempfile.mkdtemp(prefix="mcomp-import-")
    try:
        return run(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run(td):
    victim_dir = os.path.join(td, "mac-files")
    os.makedirs(victim_dir)
    victim = os.path.join(victim_dir, "precious.txt")
    with open(victim, "w") as f:
        f.write("the Mac's own file\n")
    before = open(victim, "rb").read()
    video = tiny_video(os.path.join(td, "v.mp4"))
    payload = hostile_tar(video, victim, victim_dir)

    # 1. the importer on its own
    dest = os.path.join(td, "dest")
    r = subprocess.run([sys.executable, IMPORT, dest], input=payload, capture_output=True)
    err = r.stderr.decode()
    check("the importer reads the hostile stream (exit 0) and keeps the plain files", r.returncode == 0
          and open(os.path.join(dest, "recording", "x.mp4"), "rb").read() == video
          and open(os.path.join(dest, "files", "result.txt")).read() == "the real output\n", (r.returncode, err))
    check("...no link, FIFO or device reaches the Mac", links_or_special(dest) == [], links_or_special(dest))
    check("...the link named x-contact.png, the FIFO, the hard link, the device and the folder link are left out "
          "(escape/owned.txt lands in a real folder of that name)",
          not os.path.lexists(os.path.join(dest, "recording", "x-contact.png")) and not os.path.lexists(os.path.join(dest, "pipe"))
          and not os.path.lexists(os.path.join(dest, "hard")) and not os.path.lexists(os.path.join(dest, "dev"))
          and not os.path.islink(os.path.join(dest, "escape")), sorted(os.listdir(dest)))
    check("...nothing was written through the folder link, and the Mac file is untouched",
          not os.path.exists(os.path.join(victim_dir, "owned.txt")) and open(victim, "rb").read() == before)
    check("...absolute names and .. are left out", not os.path.exists("/abs-planted.txt")
          and not os.path.exists(os.path.join(td, "up-planted.txt")))
    check("...names with escape sequences or newlines are left out", not any("\x1b" in n or "\n" in n for n in os.listdir(dest)))
    check("...what was left out is reported, escaped", "x-contact.png (symbolic link)" in err and "(FIFO)" in err
          and "(hard link)" in err and "\x1b" not in err and "\\x1b]52" in err, err)
    check("...regular files keep an executable bit only as 0755, and the stream's mtime",
          stat.S_IMODE(os.stat(os.path.join(dest, "files", "result.txt")).st_mode) & 0o111
          and int(os.stat(os.path.join(dest, "recording", "x.mp4")).st_mtime) == 1700000000)
    check("...the private staging folder is gone", not [n for n in os.listdir(dest) if n.startswith(".mcomp-import-")])

    # 2. a link already on the Mac where a file arrives is replaced, not written through
    dest2 = os.path.join(td, "dest2")
    os.makedirs(os.path.join(dest2, "recording"))
    os.symlink(victim, os.path.join(dest2, "recording", "x.mp4"))
    os.symlink(victim_dir, os.path.join(dest2, "files"))
    r = subprocess.run([sys.executable, IMPORT, dest2], input=payload, capture_output=True)
    check("a link already on the Mac where the VM's file goes: replaced by the file, its target untouched",
          r.returncode == 0 and not os.path.islink(os.path.join(dest2, "recording", "x.mp4"))
          and open(os.path.join(dest2, "recording", "x.mp4"), "rb").read() == video and open(victim, "rb").read() == before)
    check("...a folder link on the Mac isn't entered: replaced by a real folder, nothing lands in its target",
          not os.path.islink(os.path.join(dest2, "files")) and os.path.isfile(os.path.join(dest2, "files", "result.txt"))
          and sorted(os.listdir(victim_dir)) == ["precious.txt"], sorted(os.listdir(victim_dir)))

    # 3. a stream cut off part-way
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as tf:
        add_file(tf, "whole.txt", b"w" * 5000)
        add_file(tf, "cut.bin", b"c" * 200000)
    cut = b.getvalue()[:60000]
    dest3 = os.path.join(td, "dest3")
    rep = guest_import.import_tar(io.BytesIO(cut), dest3)
    check("a stream cut off part-way: the whole file kept, the cut one not, the failure reported",
          rep.failure is not None and os.path.isfile(os.path.join(dest3, "whole.txt"))
          and not os.path.exists(os.path.join(dest3, "cut.bin")), (rep.failure, os.listdir(dest3)))
    r = subprocess.run([sys.executable, IMPORT, os.path.join(td, "dest4")], input=b"", capture_output=True)
    check("an empty stream: exit 1, nothing came", r.returncode == 1 and b"nothing came" in r.stderr, r.stderr)

    # 4. contact-sheet: never through a link
    env = fake_vm(td, payload)
    vdir = os.path.join(td, "vids")
    os.makedirs(vdir)
    vid = os.path.join(vdir, "y.mp4")
    shutil.copy(os.path.join(td, "v.mp4"), vid)
    os.symlink(victim, os.path.join(vdir, "y-contact.png"))
    r = subprocess.run([CLI, "contact-sheet", vid], env=env, capture_output=True, text=True)
    check("contact-sheet refuses a default output that's a link; the Mac file is untouched", r.returncode != 0
          and "symbolic link" in r.stderr and open(victim, "rb").read() == before, (r.returncode, r.stderr))
    out = os.path.join(vdir, "z.png")
    r = subprocess.run([CLI, "contact-sheet", vid, out], env=env, capture_output=True, text=True)
    check("contact-sheet to a new file: a PNG, no temporary file left", r.returncode == 0
          and open(out, "rb").read(8) == b"\x89PNG\r\n\x1a\n" and not [n for n in os.listdir(vdir) if n.startswith(".contact")],
          (r.returncode, r.stderr, os.listdir(vdir)))

    # 5. task finish against the hostile VM, end to end
    name = time.strftime("mcomp-%Y%m%d-%H%M%S-import-test")
    host = os.path.join(LAB, "results", name)
    try:
        os.makedirs(host)
        with open(os.path.join(host, "task.json"), "w") as f:
            json.dump({"name": name, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "requested_from": td}, f)
        r = subprocess.run([CLI, "task", "finish", name], env=env, capture_output=True, text=True, timeout=120)
        g = os.path.join(host, "guest")
        check("task finish with a hostile VM: the video arrives, the link named x-contact.png doesn't",
              r.returncode == 0 and os.path.isfile(os.path.join(g, "recording", "x.mp4"))
              and links_or_special(host) == [], (r.returncode, r.stdout[-500:], r.stderr[-800:], links_or_special(host)))
        check("...the Mac file the link pointed at is untouched", open(victim, "rb").read() == before)
        sheet = os.path.join(g, "recording", "x-contact.png")
        check("...the contact sheet is a real PNG of its own", os.path.isfile(sheet) and not os.path.islink(sheet)
              and open(sheet, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
        check("...what was left out is reported", "x-contact.png (symbolic link)" in r.stderr and "\x1b" not in r.stderr,
              r.stderr[-800:])
        arts = json.load(open(os.path.join(host, "artifacts.json")))
        check("...artifacts.json lists the video and its sheet", [os.path.basename(v["path"]) for v in arts["videos"]] == ["x.mp4"]
              and [os.path.basename(p) for p in arts["contact_sheets"]] == ["x-contact.png"], arts)
    finally:
        shutil.rmtree(host, ignore_errors=True)

    # 6. get: the same importer, into a folder of your choice
    gdest = os.path.join(td, "got")
    r = subprocess.run([CLI, "get", "~/metacomputer-runs/x", gdest], env=env, capture_output=True, text=True, timeout=60)
    check("get with a hostile VM: plain files only, the Mac file untouched", r.returncode == 0
          and os.path.isfile(os.path.join(gdest, "recording", "x.mp4")) and links_or_special(gdest) == []
          and open(victim, "rb").read() == before, (r.returncode, r.stderr[-500:]))

    # 7. recordings --sync: the same importer; the listing escapes the VM's words
    # (list every recording: the checkout may already hold newer ones than the one synced here)
    rec_payload = io.BytesIO()
    with tarfile.open(fileobj=rec_payload, mode="w") as tf:
        add_dir(tf, ".")
        add_file(tf, "./run1/clip.mp4", video)
        add_file(tf, "./run1/clip.json", json.dumps({"task": "demo \x1b[2J\x1b]52;c;SGk=\x07"}).encode())
        add_special(tf, "./run1/clip-contact.png", tarfile.SYMTYPE, victim)
    with open(env["MCOMP_TEST_TAR"], "wb") as f:
        f.write(rec_payload.getvalue())
    synced = os.path.join(LAB, "recordings", "guest-runs", "run1")
    existed = os.path.exists(synced)
    try:
        r = subprocess.run([CLI, "recordings", "--sync", "100000"], env=env, capture_output=True, text=True, timeout=120)
        check("recordings --sync: the clip arrives, the link doesn't", r.returncode == 0
              and os.path.isfile(os.path.join(synced, "clip.mp4")) and not os.path.lexists(os.path.join(synced, "clip-contact.png")),
              (r.returncode, r.stderr[-400:]))
        check("...the listing shows the VM's description escaped", "run1/clip.mp4" in r.stdout and "\x1b" not in r.stdout
              and "\\x1b[2J" in r.stdout, r.stdout[-600:])
    finally:
        if not existed:
            shutil.rmtree(synced, ignore_errors=True)
    # 8. limits: a VM can't fill the Mac's disk or memory
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as tf:
        add_file(tf, "a.bin", b"a" * 300_000)
        add_file(tf, "b.bin", b"b" * 300_000)
        add_file(tf, "c.bin", b"c" * 300_000)
    big = b.getvalue()
    d8 = os.path.join(td, "dest8")
    rep = guest_import.import_tar(io.BytesIO(big), d8, max_bytes=700_000)
    check("past the size limit: the copy stops, whole files kept, the cut one removed",
          isinstance(rep.failure, guest_import.ImportLimit) and sorted(os.listdir(d8)) == ["a.bin", "b.bin"]
          and rep.bytes == 600_000, (rep.failure, os.listdir(d8)))
    d8b = os.path.join(td, "dest8b")
    rep = guest_import.import_tar(io.BytesIO(big), d8b, min_free=1 << 62)
    check("too little free space on the Mac: nothing is written", isinstance(rep.failure, guest_import.ImportLimit)
          and os.listdir(d8b) == [] and "free" in str(rep.failure), (rep.failure, os.listdir(d8b)))
    many = io.BytesIO()
    with tarfile.open(fileobj=many, mode="w") as tf:
        for i in range(30):
            add_special(tf, f"l{i}", tarfile.SYMTYPE, "/etc/passwd")
    saved = guest_import.MAX_MEMBERS, guest_import.MAX_DROPPED_KEPT
    guest_import.MAX_MEMBERS, guest_import.MAX_DROPPED_KEPT = 20, 5
    try:
        rep = guest_import.import_tar(io.BytesIO(many.getvalue()), os.path.join(td, "dest8c"))
    finally:
        guest_import.MAX_MEMBERS, guest_import.MAX_DROPPED_KEPT = saved
    check("past the entry limit: the copy stops; left-out names kept for the report are capped, the count exact",
          isinstance(rep.failure, guest_import.ImportLimit) and rep.dropped_count == 20 and len(rep.dropped) == 5,
          (rep.failure, rep.dropped_count, len(rep.dropped)))
    check("limits from the environment: VM_DISK_BYTES and IMPORT_MIN_FREE_GB; empty or bad values fall back",
          guest_import.limits_from_env({"MCOMP_IMPORT_MAX_BYTES": "1000", "MCOMP_IMPORT_MIN_FREE_GB": "2"}) == (1000, 2 << 30)
          and guest_import.limits_from_env({}) == (None, 10 << 30)
          and guest_import.limits_from_env({"MCOMP_IMPORT_MAX_BYTES": "x", "MCOMP_IMPORT_MIN_FREE_GB": "-1"}) == (None, 10 << 30))
    with open(env["MCOMP_TEST_TAR"], "wb") as f:
        f.write(big)
    with open(env["MCOMP_CONFIG"], "a") as f:
        f.write("VM_DISK_BYTES=700000\n")
    gdest2 = os.path.join(td, "got2")
    r = subprocess.run([CLI, "get", "~/x", gdest2], env=env, capture_output=True, text=True, timeout=60)
    check("get passes VM_DISK_BYTES as the limit: it stops and says why", r.returncode != 0
          and "stopped copying from the VM" in r.stderr and sorted(os.listdir(gdest2)) == ["a.bin", "b.bin"],
          (r.returncode, r.stderr[-400:], os.listdir(gdest2) if os.path.isdir(gdest2) else None))

    check("the Mac file is still untouched at the end", open(victim, "rb").read() == before)
    hostile_metadata_checks(td)
    command_checks(td)

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


class Counting:
    """A stream that counts what was read from it."""

    def __init__(self, data):
        self.b, self.n = io.BytesIO(data), 0

    def read(self, n=-1):
        d = self.b.read(n)
        self.n += len(d)
        return d


def one_member(name="ok.txt", data=b"ok\n"):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        add_file(tf, name, data)
    return b.getvalue()


def ext_header(kind, size, name="././@LongLink"):
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.size = size
    return ti.tobuf(tarfile.GNU_FORMAT)


def objects_under(root):
    return sum(len(ds) + len(fs) for _, ds, fs in os.walk(root))


def hostile_metadata_checks(td):
    # 9. what a VM can send besides file data: tar extension headers, folder floods, the stream itself
    gi = guest_import
    n = 0

    def dest():
        nonlocal n
        n += 1
        return os.path.join(td, f"meta{n}")
    for kind, label in ((tarfile.GNUTYPE_LONGNAME, "a GNU long-name header"), (tarfile.XHDTYPE, "a pax header")):
        s = Counting(ext_header(kind, 100 << 20) + b"a" * (2 << 20))
        rep = gi.import_tar(s, dest())
        check(f"{label} that claims 100 MB is refused before its payload is read (memory stays bounded)",
              isinstance(rep.failure, gi.ImportLimit) and "extension header" in str(rep.failure) and s.n < 100_000,
              (rep.failure, s.n))
    ln = tarfile.TarInfo._create_gnu_long_header("x" * 200, tarfile.GNUTYPE_LONGNAME, "utf-8", "surrogateescape")
    rep = gi.import_tar(io.BytesIO(ln * (gi.MAX_EXT_CHAIN + 1) + one_member()), dest())
    check("a chain of extension headers longer than MAX_EXT_CHAIN is refused (no recursion through them)",
          isinstance(rep.failure, gi.ImportLimit) and "in a row" in str(rep.failure), rep.failure)
    d = dest()
    rep = gi.import_tar(io.BytesIO(ln + one_member()), d)
    check("…while one long name is fine (and the name it gives is checked like any other)",
          rep.failure is None and rep.files == 1 and len(os.listdir(d)) == 1, (rep.failure, os.listdir(d)))
    gh = tarfile.TarInfo.create_pax_global_header({"comment": "x" * 40000})
    rep = gi.import_tar(io.BytesIO(gh + gh + one_member()), dest())
    check("global pax headers (they stay for the whole stream) are capped in all",
          isinstance(rep.failure, gi.ImportLimit) and "global pax" in str(rep.failure), rep.failure)
    rep = gi.import_tar(io.BytesIO(ext_header(tarfile.GNUTYPE_SPARSE, 1000, "sparse.bin") + b"\0" * 1024), dest())
    check("a GNU sparse entry (its extra headers can go on and on) is refused",
          isinstance(rep.failure, gi.ImportLimit) and "sparse" in str(rep.failure), rep.failure)
    deep = io.BytesIO()
    with tarfile.open(fileobj=deep, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for i in range(5):
            add_file(tf, "/".join(f"d{i}-{j}" for j in range(30)) + "/f.txt", b"x")
    saved = gi.MAX_OBJECTS
    gi.MAX_OBJECTS = 50
    try:
        d = dest()
        rep = gi.import_tar(io.BytesIO(deep.getvalue()), d)
    finally:
        gi.MAX_OBJECTS = saved
    check("folders a path implies count: 5 entries can't make 150 folders past the object limit (50)",
          isinstance(rep.failure, gi.ImportLimit) and "files and folders" in str(rep.failure) and objects_under(d) <= 50,
          (rep.failure, objects_under(d)))
    dirs = io.BytesIO()
    with tarfile.open(fileobj=dirs, mode="w") as tf:
        for i in range(3):
            add_dir(tf, f"only-dir-{i}")
    d = dest()
    rep = gi.import_tar(io.BytesIO(dirs.getvalue()), d, min_free=1 << 62)
    check("a stream of folders only meets the free-space floor too (nothing made)",
          isinstance(rep.failure, gi.ImportLimit) and "free" in str(rep.failure) and os.listdir(d) == [],
          (rep.failure, os.listdir(d)))
    skipped = io.BytesIO()
    with tarfile.open(fileobj=skipped, mode="w") as tf:
        add_file(tf, "/abs-left-out.bin", b"s" * (5 << 20))      # left out, but tar reads past its data
    saved = gi.MAX_MEMBERS, gi.STREAM_SLACK
    gi.MAX_MEMBERS, gi.STREAM_SLACK = 10, 0
    try:
        s = Counting(skipped.getvalue())
        rep = gi.import_tar(s, dest(), max_bytes=1000)
    finally:
        gi.MAX_MEMBERS, gi.STREAM_SLACK = saved
    check("the whole stream is capped too (data of left-out entries counts): it stops near the cap",
          isinstance(rep.failure, gi.ImportLimit) and "stream passed" in str(rep.failure) and s.n < 100_000,
          (rep.failure, s.n))
    import gzip
    d = dest()
    rep = gi.import_tar(io.BytesIO(gzip.compress(one_member())), d)
    check("a compressed stream isn't decompressed (`tar -cf` never compresses): refused, nothing made",
          isinstance(rep.failure, tarfile.ReadError) and os.listdir(d) == [], (rep.failure, os.listdir(d)))


PRODUCER = r'''
import io, sys, tarfile, time
mode = sys.argv[1]
out = sys.stdout.buffer
if mode == "endless":                        # one entry that never ends
    ti = tarfile.TarInfo("endless.bin"); ti.size = 10 << 30
    out.write(ti.tobuf(tarfile.GNU_FORMAT))
    while True:
        out.write(b"e" * (1 << 20))
if mode == "noisy":                          # 2 MB on stderr first
    for _ in range(32):
        sys.stderr.write("\x1b]52;c;SGk=\x07" + "n" * 65000 + "\n")
    sys.stderr.flush()
with tarfile.open(fileobj=out, mode="w|") as tf:
    for n in ("a", "b", "c"):
        data = n.encode() * 300_000
        ti = tarfile.TarInfo(n + ".bin"); ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
out.flush()
if mode == "noisy":
    time.sleep(30)                           # keeps the connection open, as a VM could
'''


def command_checks(td):
    # 10. the GUI test harness's copy: streamed into the importer, with the same limits, the command reaped
    gi = guest_import
    prod = os.path.join(td, "producer.py")
    with open(prod, "w") as f:
        f.write(PRODUCER)
    d = os.path.join(td, "cmd1")
    t0 = time.time()
    rep, rc, err = gi.import_from_command([sys.executable, prod, "noisy"], d, max_bytes=700_000)
    took = time.time() - t0
    check("from a command: stops at the size limit, keeps the whole files, stops the command at once",
          isinstance(rep.failure, gi.ImportLimit) and sorted(os.listdir(d)) == ["a.bin", "b.bin"] and rc != 0
          and took < 10, (rep.failure, os.listdir(d), rc, took))
    import wirelimits
    check("…and of its 2 MB of stderr only a short tail is kept", 0 < len(err) <= wirelimits.STDERR_KEEP, len(err))
    d = os.path.join(td, "cmd2")
    t0 = time.time()
    rep, rc, err = gi.import_from_command([sys.executable, prod, "endless"], d, max_bytes=1_000_000)
    check("an entry that never ends: cut off at the limit, the command killed, nothing half-written kept",
          isinstance(rep.failure, gi.ImportLimit) and os.listdir(d) == [] and rc != 0 and time.time() - t0 < 10,
          (rep.failure, os.listdir(d), rc))
    d = os.path.join(td, "cmd3")
    rep, rc, err = gi.import_from_command([sys.executable, prod, "quiet"], d, max_bytes=10_000_000)
    check("a stream that arrives whole: every file, the command's own exit code",
          rep.failure is None and sorted(os.listdir(d)) == ["a.bin", "b.bin", "c.bin"] and rc == 0, (rep.failure, rc))
    check("limits for Python callers come from the settings (VM_DISK_BYTES, IMPORT_MIN_FREE_GB)",
          gi.limits_from_config({"VM_DISK_BYTES": "1000", "IMPORT_MIN_FREE_GB": "2"}) == (1000, 2 << 30)
          or bool(os.environ.get("MCOMP_IMPORT_MAX_BYTES")))
    src = open(os.path.join(LAB, "lib", "harness.py")).read()
    check("the harness copies through import_from_command with those limits (no capture_output of the stream)",
          "import_from_command" in src and "limits_from_config" in src and "capture_output=True, timeout=600" not in src)


if __name__ == "__main__":
    sys.exit(main())
