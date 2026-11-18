#!/usr/bin/env python3
"""APT key pinning (scripts/pin-gpg-key.sh, used by scripts/fetch-artifacts.sh). No VM;
needs gpg. Throwaway keys are made in a temporary folder.

    python3 tests/test_key_pinning.py

It checks that only the key with the pinned fingerprint reaches the file the guest
installs: an extra key appended to the bundle (or put first) is left out and named, a
bundle without the pinned key is refused, a subkey grafted onto the pinned key without
its binding signature is dropped, and the real cached Mozilla and Google bundles keep
their key and all its subkeys.
"""
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = os.path.join(LAB, "scripts", "pin-gpg-key.sh")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def gpg(home, *args, data=None):
    return subprocess.run(["gpg", "--batch", "--homedir", home, *args], input=data, capture_output=True)


def keys_in(path):
    """[(kind, fingerprint)] for each primary key and subkey in a key file."""
    r = subprocess.run(["gpg", "--batch", "--with-colons", "--show-keys", path], capture_output=True, text=True)
    out, kind = [], None
    for line in r.stdout.splitlines():
        f = line.split(":")
        if f[0] in ("pub", "sub"):
            kind = f[0]
        elif f[0] == "fpr" and kind:
            out.append((kind, f[9]))
            kind = None
    return out


def packets(blob):
    """Split binary OpenPGP data into (tag, bytes) packets (old and new formats)."""
    out, i = [], 0
    while i < len(blob):
        b = blob[i]
        if b & 0x40:                      # new format
            tag, j = b & 0x3F, i + 1
            l0 = blob[j]
            if l0 < 192:
                n, j = l0, j + 1
            elif l0 < 224:
                n, j = ((l0 - 192) << 8) + blob[j + 1] + 192, j + 2
            else:
                n, j = int.from_bytes(blob[j + 1:j + 5], "big"), j + 5
        else:                             # old format
            tag, lt, j = (b >> 2) & 0x0F, b & 3, i + 1
            size = {0: 1, 1: 2, 2: 4}[lt]
            n, j = int.from_bytes(blob[j:j + size], "big"), j + size
        out.append((tag, blob[i:j + n]))
        i = j + n
    return out


def main():
    if not shutil.which("gpg"):
        print("SKIP: no gpg")
        return 0
    td = tempfile.mkdtemp(prefix="mcomp-pin-", dir="/tmp")
    home = os.path.join(td, "g")
    os.makedirs(home, mode=0o700)
    try:
        return run(td, home)
    finally:
        subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True)
        shutil.rmtree(td, ignore_errors=True)


def run(td, home):
    for uid in ("Pinned Test <pinned@example.invalid>", "Extra Test <extra@example.invalid>"):
        r = gpg(home, "--pinentry-mode", "loopback", "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign", "never")
        assert r.returncode == 0, r.stderr
    fprs = {}
    for line in gpg(home, "--with-colons", "--list-keys").stdout.decode().splitlines():
        f = line.split(":")
        if f[0] == "pub":
            want = True
        elif f[0] == "fpr" and want:
            fprs[len(fprs)] = f[9]
            want = False
    pinned, extra = fprs[0], fprs[1]
    r = gpg(home, "--pinentry-mode", "loopback", "--passphrase", "", "--quick-add-key", extra, "ed25519", "sign", "never")
    assert r.returncode == 0, r.stderr
    a = gpg(home, "--armor", "--export", pinned).stdout
    b = gpg(home, "--armor", "--export", extra).stdout

    def bundle(name, data):
        p = os.path.join(td, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def pin(src, fpr=pinned):
        out = os.path.join(td, "out.asc")
        if os.path.exists(out):
            os.remove(out)
        r = subprocess.run([PIN, src, fpr, out], capture_output=True, text=True)
        return r, (keys_in(out) if os.path.exists(out) else None)

    r, got = pin(bundle("appended.asc", a + b))
    check("a bundle with an extra key appended: only the pinned key is kept", r.returncode == 0
          and got == [("pub", pinned)], (r.returncode, r.stderr, got))
    check("...and the extra key is named on stderr", extra in r.stderr, r.stderr)
    r, got = pin(bundle("first.asc", b + a))
    check("an extra key first, the pinned one second: only the pinned key is kept", r.returncode == 0
          and got == [("pub", pinned)], (r.returncode, got))
    r, got = pin(bundle("none.asc", b))
    check("a bundle without the pinned key: refused, nothing written", r.returncode == 1 and got is None
          and "no key with the pinned fingerprint" in r.stderr, (r.returncode, r.stderr))
    r, got = pin(bundle("junk.asc", b"not a key\n"))
    check("a file that isn't a key: refused", r.returncode == 1 and got is None, (r.returncode, r.stderr))
    r, got = pin(bundle("short.asc", a), "1234")
    check("a fingerprint that isn't 40 hex digits: refused", r.returncode == 2 and got is None, r.stderr)
    r, got = pin(bundle("lower.asc", a), pinned.lower())
    check("a lower-case pinned fingerprint works the same", r.returncode == 0 and got == [("pub", pinned)], got)

    # A subkey of the extra key grafted onto the pinned key (its binding signature is the extra key's).
    ab = gpg(home, "--export", pinned).stdout
    bb = packets(gpg(home, "--export", extra).stdout)
    graft = b""
    for k, (tag, raw) in enumerate(bb):
        if tag == 14:                                  # public subkey + the signature after it
            graft = raw + (bb[k + 1][1] if k + 1 < len(bb) and bb[k + 1][0] == 2 else b"")
    r, got = pin(bundle("graft.gpg", ab + graft))
    check("a subkey grafted onto the pinned key without a valid binding: dropped", graft and r.returncode == 0
          and got == [("pub", pinned)], (len(graft), r.returncode, r.stderr, got))

    # The real downloads, when this checkout has them, keep their key and every subkey.
    for name, src, fpr in (("Mozilla", os.path.join(LAB, ".cache", "mozilla", "repo-signing-key.gpg"),
                            "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3"),
                           ("Google", os.path.join(LAB, ".cache", "google", "linux_signing_key.pub"),
                            "EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796")):
        if not os.path.exists(src):
            print(f"SKIP {name}: no cached bundle ({src})")
            continue
        want = keys_in(src)
        r, got = pin(src, fpr)
        check(f"{name}'s real bundle: the pinned key with all {len(want) - 1} subkeys, unchanged", r.returncode == 0
              and got == want and got[0] == ("pub", fpr), (r.stderr, got))
        r, got = pin(bundle(f"{name}-plus.asc", open(src, "rb").read() + b"\n" + b), fpr)
        check(f"{name}'s real bundle with an extra key appended: the extra key is left out", r.returncode == 0
              and got == want, (r.stderr, got))

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
