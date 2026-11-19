#!/usr/bin/env python3
"""Offline checks for bringing logins into the vault: `import-passwords` (Chrome's saved passwords, and CSV
exports from Chrome, the macOS Passwords app, 1Password and Bitwarden) and `import-session` (a site's
cookies from Chrome, sealed, then loaded into a VM profile's own cookie store).

No VM and no real Chrome: the profiles here are synthetic. This test builds a Login Data and a Cookies
database with SQLite and encrypts their values as Chrome on macOS does, with a test key (AES-128-CBC under
PBKDF2-SHA1 of the "Safe Storage" password). A TEST build of mcomp-vault reads them through
MCOMP_VAULT_TEST_CHROME_DIR and takes the test key from MCOMP_VAULT_TEST_CHROME_KEY instead of the Keychain.
Release builds don't contain either switch, and test builds never fall back to the real Chrome folder or the
Keychain: both checked here. Same stub and pseudo-terminal as tests/test_secrets.py; every value a dummy.

Run: python3 tests/test_vault_import.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build [MCOMP_VAULT_REL_BIN=/path/to/release-build] python3 tests/test_vault_import.py
"""
import base64
import hashlib
import json
import os
import pwd
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
SITE = ts.SITE
CHROME_KEY = "dummy safe storage password"
PW_GH, PW_GH2, PW_EX, PW_LOCAL, PW_NEW = "Dummy-gh-pass-1", "Dummy-gh-pass-2", "Dummy-ex-pass-3", "Dummy-local-pass-4", "Dummy-changed-5"
CSV_PW = ['Dummy,csv "quoted"\npass-6', "Dummy-csv-pass-7", "Dummy-csv-pass-8", "Dummy-csv-pass-9"]
COOKIE_GH, COOKIE_LOCAL, COOKIE_SID = "dummy-gh-cookie-value", "dummy-local-cookie-value", "dummy-google-sid-value"
SEED = ts.SEED
SEED2 = "GEZDGNBVGY3TQOJQ"
PW_TW = "Dummy-tw-pass-10"
LEAK = "SECRET-FROM-GUEST-7731"
SEED3 = "MFRGGZDFMZTWQ2LK"
VALUES = [PASS, CHROME_KEY, PW_GH, PW_GH2, PW_EX, PW_LOCAL, PW_NEW, *CSV_PW, COOKIE_GH, COOKIE_LOCAL, COOKIE_SID, SEED, SEED2, SEED3, PW_TW, LEAK]
outputs = []
OPENSSL = shutil.which("openssl") or "/usr/bin/openssl"


def pbkdf2(password, rounds):
    return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", rounds, 16)


def cbc(key, data, decrypt=False):
    args = [OPENSSL, "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", "20" * 16] + (["-d"] if decrypt else [])
    r = subprocess.run(args, input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode())
    return r.stdout


def chrome_enc(value_bytes, key):
    return b"v10" + cbc(key, value_bytes)


MAC_KEY = pbkdf2(CHROME_KEY, 1003)
LINUX_KEY = pbkdf2("peanuts", 1)

LOGINS_SQL = """CREATE TABLE logins (origin_url VARCHAR NOT NULL, action_url VARCHAR, username_element VARCHAR,
 username_value VARCHAR, password_element VARCHAR, password_value BLOB, submit_element VARCHAR,
 signon_realm VARCHAR NOT NULL, date_created INTEGER NOT NULL, blacklisted_by_user INTEGER NOT NULL,
 scheme INTEGER NOT NULL, password_type INTEGER, times_used INTEGER, id INTEGER PRIMARY KEY AUTOINCREMENT)"""
COOKIES_SQL = """CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
 name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL, expires_utc INTEGER NOT NULL,
 is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL, last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL,
 is_persistent INTEGER NOT NULL, priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
 source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL, source_type INTEGER NOT NULL,
 has_cross_site_ancestor INTEGER NOT NULL)"""
COOKIES_IDX = "CREATE UNIQUE INDEX cookies_unique_index ON cookies(host_key, top_frame_site_key, name, path, source_scheme, source_port)"


def make_login_data(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute(LOGINS_SQL)
    for url, user, pw, black in rows:
        blob = pw if isinstance(pw, bytes) else (chrome_enc(pw.encode(), MAC_KEY) if pw else b"")
        db.execute("INSERT INTO logins (origin_url, username_value, password_value, signon_realm, date_created, blacklisted_by_user, scheme)"
                   " VALUES (?,?,?,?,?,?,0)", (url, user, blob, url, 13300000000000000, black))
    db.commit()
    db.close()


def make_cookies(path, rows, version=24, key=MAC_KEY):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR)")
    db.execute("INSERT INTO meta VALUES ('version', ?), ('last_compatible_version', '24')", (str(version),))
    db.execute(COOKIES_SQL)
    db.execute(COOKIES_IDX)
    for i, (host, name, value) in enumerate(rows):
        plain = (hashlib.sha256(host.encode()).digest() if version >= 24 else b"") + value.encode()
        db.execute("INSERT INTO cookies VALUES (?,?,'',?,'',?,'/',?,1,1,?,1,1,1,0,2,443,?,0,0)",
                   (13300000000000000 + i, host, name, b"v10" + cbc(key, plain), 13400000000000000, 13300000000000001, 13300000000000002))
    db.commit()
    db.close()


def main():
    td = tempfile.mkdtemp(prefix="mcvi-", dir="/tmp")
    procs = []
    try:
        run(td, procs)
    finally:
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
            if isinstance(p, ts.Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if all(ts.results) else 1


def run(td, procs):
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    REL = os.environ.get("MCOMP_VAULT_REL_BIN")
    if not REL:
        REL = os.path.join(td, "bin", "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--out", REL], capture_output=True, text=True)
        check("build: release binary", r.returncode == 0, r.stderr[-800:])
    blob = open(REL, "rb").read()
    check("the release build has neither Chrome stand-in (MCOMP_VAULT_TEST_CHROME_DIR, MCOMP_VAULT_TEST_CHROME_KEY)",
          b"MCOMP_VAULT_TEST_CHROME_DIR" not in blob and b"MCOMP_VAULT_TEST_CHROME_KEY" not in blob and b"MCOMP_VAULT_TEST_" not in blob)
    tblob = open(BIN, "rb").read()
    check("the test build has them", b"MCOMP_VAULT_TEST_CHROME_DIR" in tblob and b"MCOMP_VAULT_TEST_CHROME_KEY" in tblob)
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []

    # --- synthetic Chrome profiles ------------------------------------------------------------------------
    chrome = os.path.join(td, "chrome")
    login_rows = [("https://github.com/login", "dummy-user-qx7", PW_GH, 0),
                  ("https://github.com/", "other-user", PW_GH2, 0),
                  ("https://example.com/", "a.person@example.com", PW_EX, 0),
                  ("http://insecure.example/", "u", "Dummy-ignored", 0),
                  ("android://com.example.app/", "u", "Dummy-ignored", 0),
                  ("https://never.example/", "", "", 1),
                  ("https://keyring.example/", "u", b"v11" + b"\x00" * 16, 0),
                  (SITE + "/login.html", "local-user", PW_LOCAL, 0)]
    make_login_data(os.path.join(chrome, "Default", "Login Data"), login_rows)
    make_login_data(os.path.join(chrome, "Profile 1", "Login Data"), [("https://second.example/", "p1-user", "Dummy-p1-pass", 0)])
    # Profile 2: `logins` is a view (a file someone else prepared), not Chrome's table.
    os.makedirs(os.path.join(chrome, "Profile 2"))
    vdb = sqlite3.connect(os.path.join(chrome, "Profile 2", "Login Data"))
    vdb.execute("CREATE TABLE real_logins (origin_url VARCHAR, username_value VARCHAR, password_value BLOB, blacklisted_by_user INTEGER)")
    vdb.execute("INSERT INTO real_logins VALUES ('https://view.example/', 'view-user', ?, 0)", (chrome_enc(b"Dummy-view-pass", MAC_KEY),))
    vdb.execute("CREATE VIEW logins AS WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
                "SELECT origin_url || i AS origin_url, username_value, password_value, blacklisted_by_user FROM real_logins, n")
    vdb.commit()
    vdb.close()
    # Profile 3: a cookie store older than version 24 (no domain inside the values).
    make_cookies(os.path.join(chrome, "Profile 3", "Network", "Cookies"), [("github.com", "user_session", COOKIE_GH)], version=23)
    # Profile 4: an identity provider's cookie relabeled as github.com's (its encrypted value still names .google.com).
    p4 = os.path.join(chrome, "Profile 4", "Network", "Cookies")
    make_cookies(p4, [(".google.com", "SID", COOKIE_SID), ("github.com", "user_session", COOKIE_GH)])
    rdb = sqlite3.connect(p4)
    rdb.execute("UPDATE cookies SET host_key = 'github.com', name = 'stolen' WHERE name = 'SID'")
    rdb.commit()
    rdb.close()
    make_cookies(os.path.join(chrome, "Default", "Network", "Cookies"),
                 [("github.com", "user_session", COOKIE_GH), (".github.com", "_gh_sess", COOKIE_GH + "2"),
                  ("localhost", "mc_session", COOKIE_LOCAL), (".google.com", "SID", COOKIE_SID),
                  ("accounts.google.com", "LSID", COOKIE_SID + "2"), ("other.example", "x", "Dummy-unrelated")])

    # --- the stand-in VM and the vault ----------------------------------------------------------------------
    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(ts.STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_SSH": stub, "MCOMP_VAULT_TEST_CHROME_DIR": chrome, "MCOMP_VAULT_TEST_CHROME_KEY": CHROME_KEY}
    sock = os.path.join(vdir, "vault.sock")
    alog = os.path.join(vdir, "audit.jsonl")

    def client(*args, extra=None, timeout=120, drop=()):
        e = {k: v for k, v in dict(env, **(extra or {})).items() if k not in drop}
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, timeout=timeout)
        outputs.append(r.stdout + r.stderr)
        return r

    def background(*args):
        return subprocess.Popen([BIN, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)

    def finish(proc, timeout=120):
        out, err = proc.communicate(timeout=timeout)
        outputs.append(out + err)
        return proc.returncode, out, err

    def js(text):
        try:
            return json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError, AttributeError):
            return {}

    def tty(argv, answers, extra=None, timeout=90, drop=()):
        e = {k: v for k, v in dict(env, **(extra or {})).items() if k not in drop}
        p = ts.Pty(argv, e)
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        outputs.append(p.text())
        return rc, p.text()

    def secrets():
        r = client("list", "--json")
        return {s["name"]: s for s in json.loads(r.stdout or "{}").get("secrets", [])} if r.returncode == 0 else {}

    def vault_raw():
        return open(os.path.join(vdir, "vault.json")).read()

    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("init", rc == 0, out)
    rc, out = tty([BIN, "bind", "ivm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill",
                   "--fill-user", "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe],
                  [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind the stand-in VM", rc == 0, out)

    # --- the stand-ins can't reach anything real ------------------------------------------------------------
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--dry-run"], [("", None)], drop=("MCOMP_VAULT_TEST_CHROME_DIR",))
    check("a test build without MCOMP_VAULT_TEST_CHROME_DIR refuses (it never reads the real Chrome folder)",
          rc != 0 and "MCOMP_VAULT_TEST_CHROME_DIR" in out, out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--dry-run"], [("", None)],
                  extra={"MCOMP_VAULT_TEST_CHROME_DIR": os.path.join(real_home, "Library/Application Support/Google/Chrome")})
    check("...and refuses a stand-in folder inside ~/Library", rc != 0 and "outside ~/Library" in out, out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome"], [("Vault passphrase", PASS + "\n")], drop=("MCOMP_VAULT_TEST_CHROME_KEY",))
    check("a test build without MCOMP_VAULT_TEST_CHROME_KEY refuses (it never asks the Keychain)", rc != 0
          and "never asks the Keychain" in out and not secrets(), out)

    # --- import-passwords from Chrome ---------------------------------------------------------------------
    r = client("import-passwords", "--browser", "chrome")
    check("import-passwords without a terminal: refused", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    r = client("import-passwords", "--browser", "firefox")
    check("--browser: only chrome", r.returncode == 64, r.stderr)
    before = vault_raw()
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--dry-run"], [("Dry run", None)])
    check("--dry-run: sites, masked usernames, proposed names and what's skipped; no passphrase asked, nothing saved",
          rc == 0 and "https://github.com" in out and "a***@example.com" in out and "github.com-2" in out and "Vault passphrase" not in out
          and "not an https site" in out and "an app's login" in out and "'never save'" in out and vault_raw() == before
          and "a.person@example.com" not in out, out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome"], [("Vault passphrase", PASS + "\n")])
    check("import from Chrome: 4 new logins, the v11 one and the others skipped, values never shown", rc == 0
          and "Imported 4 new" in out and "can't open" in out and not any(v in out for v in VALUES), out)
    u = ts.Pty([BIN, "unlock", "--for", "10m", *allow], dict(env, MCOMP_VAULT_TEST_LISTENERS=my_name, MCOMP_VAULT_TEST_PROMPT_TIMEOUT="6"))
    procs.append(u)
    u.expect("Vault passphrase: ")
    u.send(PASS + "\n")
    u.expect("unlocked until")
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    s = secrets()
    check("the imported logins: one per site and username, each asking before every use (approval each)",
          set(s) >= {"github.com", "github.com-2", "example.com", "127.0.0.1"} and all(s[n]["approval"] == "each" for n in s)
          and s["github.com"]["origins"] == ["https://github.com"] and s["example.com"]["username"] == "a.person@example.com"
          and s["127.0.0.1"]["origins"] == [SITE], s)
    check("the vault file holds none of them in the clear", not any(v in vault_raw() for v in (PW_GH, PW_EX, "github.com")))
    n = len(calls())
    proc = background("fill", "github.com", "--vm", "ivm", "--field", "password")
    u.expect("Type github.com to allow")
    u.send("github.com\n")
    rc, out, _ = finish(proc)
    sent = [json.loads(c["stdin"]) for c in calls()[n:]]
    check("an imported login asks, then fills the password Chrome had saved (decrypted with the Safe Storage key)", rc == 0
          and sent[-1].get("value") == PW_GH and PW_GH not in out, out)
    # again: unchanged; a changed password: updated (a rotation); --site; another profile
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome"], [("Vault passphrase", PASS + "\n")])
    check("importing again: nothing new, nothing duplicated", rc == 0 and "Imported 0 new, updated 0, left 4" in out
          and len(secrets()) == len(s), out)
    login_rows[0] = ("https://github.com/login", "dummy-user-qx7", PW_NEW, 0)
    make_login_data(os.path.join(chrome, "Default", "Login Data"), login_rows)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--site", "github.com"], [("Vault passphrase", PASS + "\n")])
    s2 = secrets()
    check("a password changed in Chrome: updated in place (recorded as a rotation); --site keeps to that site", rc == 0
          and "updated 1" in out and "example.com" not in out.split("logins to bring in")[-1] and s2["github.com"].get("rotated")
          and s2["github.com"]["approval"] == "each", out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--profile", "Profile 1"], [("Vault passphrase", PASS + "\n")])
    check("--profile reads another Chrome profile", rc == 0 and "second.example" in secrets(), out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--profile", "Profile 2", "--dry-run"], [("", None)])
    check("a Login Data whose logins is a view (here over a recursive query) is refused before any row is read",
          rc != 0 and "isn't an ordinary table" in out and "view.example" not in out, out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--profile", "../x"], [("", None)])
    check("--profile: a folder name only", rc == 64, out)
    rc, out = tty([BIN, "import-passwords", "--browser", "chrome", "--site", "github.com"], [("Vault passphrase", PASS + "\n")],
                  extra={"MCOMP_VAULT_TEST_CHROME_KEY": "not the right key"})
    check("the wrong Safe Storage key: nothing is imported or changed, every login skipped as unreadable", rc == 0
          and "Imported 0 new, updated 0" in out and "can't open" in out and secrets()["github.com"].get("rotated") == s2["github.com"].get("rotated"), out)

    # --- CSV exports ---------------------------------------------------------------------------------------
    csvdir = os.path.join(td, "csv")
    os.makedirs(csvdir)

    def csv_file(name, text):
        p = os.path.join(csvdir, name)
        open(p, "w", newline="").write(text)
        return p

    def q(s):
        return '"' + s.replace('"', '""') + '"'

    chrome_csv = csv_file("chrome.csv", "name,url,username,password,note\n"
                          f"csv.example,https://csv.example/login,csv-user,{q(CSV_PW[0])},\n"
                          "insecure,http://plain.example/,u,Dummy-ignored,\n")
    apple_csv = csv_file("passwords.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                         f"apple.example,https://apple.example/,apple-user,{CSV_PW[1]},,otpauth://totp/x:apple-user?secret={SEED}&issuer=x\n"
                         f"sha.example,https://sha.example/,sha-user,{CSV_PW[1]},,otpauth://totp/x?secret={SEED}&algorithm=SHA256\n")
    op_csv = csv_file("1password.csv", "Title,Url,Username,Password,OTPAuth,Favorite,Archived,Tags,Notes\n"
                      f"op.example,https://op.example/,op-user,{CSV_PW[2]},,false,false,,\n")
    bw_csv = csv_file("bitwarden.csv", "﻿folder,favorite,type,name,notes,fields,reprompt,login_uri,login_username,login_password,login_totp\n"
                      f",,login,bw.example,,,0,\"https://bw.example/,https://www.bw.example/\",bw-user,{CSV_PW[3]},{SEED}\n"
                      ",,note,a note,secret note text,,0,,,,\n")
    rc, out = tty([BIN, "import-passwords", "--csv", chrome_csv, "--dry-run"], [("Dry run", None)])
    check("a Chrome CSV, dry run: what it would import, no values", rc == 0 and "Chrome export" in out and "csv.example" in out
          and "not an https site" in out and CSV_PW[0] not in out, out)
    rc, out = tty([BIN, "import-passwords", "--csv", chrome_csv], [("Vault passphrase", PASS + "\n")])
    n = len(calls())
    proc = background("fill", "csv.example", "--vm", "ivm", "--field", "password")
    u.expect("Type csv.example to allow")
    u.send("csv.example\n")
    finish(proc)
    check("a Chrome CSV: a password with a comma, quotes and a line break comes in whole", rc == 0
          and json.loads(calls()[-1]["stdin"]).get("value") == CSV_PW[0], out)
    rc, out = tty([BIN, "import-passwords", "--csv", apple_csv], [("Vault passphrase", PASS + "\n")])
    s = secrets()
    check("a Passwords app CSV: its OTPAuth becomes a TOTP secret beside the login; an unsupported one is skipped and said",
          rc == 0 and "Passwords app export" in out and s.get("apple.example-otp", {}).get("kind") == "totp"
          and s["apple.example-otp"]["origins"] == ["https://apple.example"] and "sha.example" in s and "sha.example-otp" not in s
          and "a 2FA seed this vault can't use" in out and "sha.example-otp" not in out, (out, sorted(s)))
    n = len(calls())
    proc = background("fill", "apple.example-otp", "--vm", "ivm", "--field", "otp")
    u.expect("Type apple.example-otp to allow")
    u.send("apple.example-otp\n")
    finish(proc)
    t0 = int(time.time())
    check("...and it fills the current code", json.loads(calls()[-1]["stdin"]).get("value") in {ts.totp(SEED, t0 + d) for d in (-30, 0, 30)})
    rc, out = tty([BIN, "import-passwords", "--csv", op_csv], [("Vault passphrase", PASS + "\n")])
    check("a 1Password CSV", rc == 0 and "1Password export" in out and "op.example" in secrets(), out)
    rc, out = tty([BIN, "import-passwords", "--csv", bw_csv], [("Vault passphrase", PASS + "\n")])
    s = secrets()
    check("a Bitwarden CSV: every URI of a login, its TOTP, notes skipped", rc == 0 and "Bitwarden export" in out
          and s.get("bw.example", {}).get("origins") == ["https://bw.example", "https://www.bw.example"] and "bw.example-otp" in s
          and "not a login" in out, (out, s.get("bw.example")))
    many = ",".join(f"https://h{i:05d}.many.example/" for i in range(12000))
    big_csv = csv_file("big.csv", "folder,favorite,type,name,notes,fields,reprompt,login_uri,login_username,login_password,login_totp\n"
                       f",,login,many,,,0,\"{many}\",many-user,Dummy-many-pass,\n")
    rc, out = tty([BIN, "import-passwords", "--csv", big_csv], [("Vault passphrase", PASS + "\n")])
    s = secrets()
    r = client("audit", "--verify")
    check("a login with 12,000 sites in one cell comes in with its first 20, says so, and the audit chain still verifies",
          rc == 0 and len(s.get("h00000.many.example", {}).get("origins", [])) == 20 and "past the first 20" in out
          and r.returncode == 0, (out[-400:], len(s.get("h00000.many.example", {}).get("origins", [])), r.stdout[-300:] + r.stderr[-300:]))
    tw1 = csv_file("tw1.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                   f"tw,https://tw.example/,alice-tw,{PW_TW},,\n"
                   f"tw,https://tw.example/,bob-tw,{PW_TW}b,,otpauth://totp/x:bob?secret={SEED}&issuer=x\n")
    rc, out = tty([BIN, "import-passwords", "--csv", tw1], [("Vault passphrase", PASS + "\n")])
    s = secrets()
    bob_otp = next((n for n in s if s[n]["kind"] == "totp" and n.startswith("tw.example")), "")
    tw2 = csv_file("tw2.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                   f"tw,https://tw.example/,alice-tw,{PW_TW},,otpauth://totp/x:alice?secret={SEED2}&issuer=x\n")
    rc2, out2 = tty([BIN, "import-passwords", "--csv", tw2], [("Vault passphrase", PASS + "\n")])
    s2 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    new_otp = [x for x in recs if x.get("op") == "set" and x.get("kind") == "totp" and x.get("totp_for") == "tw.example"]
    check("a 2FA seed for one account never replaces another account's code on the same site: alice's comes in as her own, "
          "bob's is untouched, and the new code has its own audit record", rc == 0 and rc2 == 0 and bob_otp == "tw.example-2-otp"
          and "tw.example-otp" in s2 and not s2.get(bob_otp, {}).get("rotated") and len(new_otp) == 1,
          (out2[-500:], sorted(n for n in s2 if n.startswith("tw.")), s2.get(bob_otp)))
    # N5: a 2FA code belongs to its login's id, which is never reused; a name is.
    tz_a = csv_file("tz-a.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                    f"tz,https://tz.example/,alice-tz,{PW_TW}a,,otpauth://totp/x:alice?secret={SEED}&issuer=x\n")
    tz_b = csv_file("tz-b.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                    f"tz,https://tz.example/,bob-tz,{PW_TW}b,,otpauth://totp/x:bob?secret={SEED2}&issuer=x\n")
    rc1, o1 = tty([BIN, "import-passwords", "--csv", tz_a], [("Vault passphrase", PASS + "\n")])
    s1 = secrets()
    rc2, o2 = tty([BIN, "delete", "tz.example"], [("Vault passphrase", PASS + "\n")])
    rc3, o3 = tty([BIN, "import-passwords", "--csv", tz_b], [("Vault passphrase", PASS + "\n")])
    s3 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    alice_rotated = [x for x in recs if x.get("op") == "rotate" and x.get("name") == "tz.example-otp"]
    check("N5: Alice's login deleted, then Bob's imported under the same name: Bob gets a 2FA code of his own, Alice's is "
          "left as it was (the delete said it belongs to no login now)", rc1 == rc2 == rc3 == 0 and "tz.example-otp" in s1
          and "tied to no login" in o2 and not alice_rotated and not s3.get("tz.example-otp", {}).get("rotated")
          and "tz.example-otp-2" in s3 and any(x.get("op") == "delete" and x.get("totp_unlinked") == ["tz.example-otp"] for x in recs),
          (o2[-300:], o3[-400:], sorted(n for n in s3 if n.startswith("tz.")), alice_rotated))
    rc4, o4 = tty([BIN, "set", "tz.example-otp-2", "--kind", "totp", "--origin", "https://tz.example", "--replace"],
                  [("Vault passphrase", PASS + "\n"), ("TOTP seed for", SEED3 + "\n")])
    tz_b2 = csv_file("tz-b2.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                     f"tz,https://tz.example/,bob-tz,{PW_TW}b,,otpauth://totp/x:bob?secret={SEED2}&issuer=x\n")
    rc5, o5 = tty([BIN, "import-passwords", "--csv", tz_b2], [("Vault passphrase", PASS + "\n")])
    s5 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    check("...and `set --replace` on a 2FA code keeps the login it belongs to: Bob's next import rotates that code, it "
          "doesn't make another", rc4 == 0 and rc5 == 0 and "tz.example-otp-3" not in s5
          and any(x.get("op") == "rotate" and x.get("name") == "tz.example-otp-2" and x.get("kind") == "totp"
                  and x.get("imported_from") for x in recs), (o5[-400:], sorted(n for n in s5 if n.startswith("tz."))))
    # P2: `set --replace` that turns a login into another account's unties its 2FA code.
    tx_a = csv_file("tx-a.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                    f"tx,https://tx.example/,alice-tx,{PW_TW}a,,otpauth://totp/x:alice?secret={SEED}&issuer=x\n")
    tx_b = csv_file("tx-b.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                    f"tx,https://tx.example/,bob-tx,{PW_TW}b,,otpauth://totp/x:bob?secret={SEED2}&issuer=x\n")
    rc1, o1 = tty([BIN, "import-passwords", "--csv", tx_a], [("Vault passphrase", PASS + "\n")])
    rc2, o2 = tty([BIN, "set", "tx.example", "--kind", "password", "--origin", "https://tx.example", "--username", "bob-tx", "--replace"],
                  [("Vault passphrase", PASS + "\n"), ("Password for tx.example", PW_TW + "b\n"), ("same password again", PW_TW + "b\n")])
    rc3, o3 = tty([BIN, "import-passwords", "--csv", tx_b], [("Vault passphrase", PASS + "\n")])
    s3 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    check("P2: a login replaced with another account's username (set --replace) unties Alice's 2FA code and says so; Bob's "
          "import then makes his own code and leaves hers alone", rc1 == rc2 == rc3 == 0 and "No longer tied to a login" in o2
          and not any(x.get("op") == "rotate" and x.get("name") == "tx.example-otp" for x in recs) and "tx.example-otp-2" in s3
          and any(x.get("op") == "rotate" and x.get("name") == "tx.example" and x.get("totp_unlinked") == ["tx.example-otp"] for x in recs),
          (o2[-300:], o3[-300:], sorted(n for n in s3 if n.startswith("tx."))))
    # P3: a 2FA code an earlier build tied to its login by name is tied by the login's id when the vault opens.
    ty = csv_file("ty.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                  f"ty,https://ty.example/,alice-ty,{PW_TW}y,,otpauth://totp/x:alice?secret={SEED}&issuer=x\n")
    ty2 = csv_file("ty2.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                   f"ty,https://ty.example/,alice-ty,{PW_TW}y,,otpauth://totp/x:alice?secret={SEED2}&issuer=x\n")
    rc1, o1 = tty([BIN, "import-passwords", "--csv", ty], [("Vault passphrase", PASS + "\n")])
    rc2, o2 = tty([BIN, "test-legacy-totp-link", "ty.example-otp", "ty.example"], [("Vault passphrase", PASS + "\n")])
    rc3, o3 = tty([BIN, "import-passwords", "--csv", ty2], [("Vault passphrase", PASS + "\n")])
    s3 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    check("P3: a code tied by name (as an earlier build did) is tied by its login's id when the vault opens, and the next "
          "import replaces that code instead of making another", rc1 == rc2 == rc3 == 0
          and "ty.example-otp: tied to the login ty.example by its id now" in o3 and "ty.example-otp-2" not in s3
          and any(x.get("op") == "rotate" and x.get("name") == "ty.example-otp" and x.get("kind") == "totp" for x in recs)
          and any(x.get("op") == "totp_links" for x in recs), (o2[-200:], o3[-400:], sorted(n for n in s3 if n.startswith("ty."))))
    # P2: two logins saved by an earlier build (no ids) with their codes tied by name: migrated once, one id each, and the
    # import right after replaces each code instead of making new ones.
    tl = csv_file("tl.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                  f"tl,https://tl1.example/,alice-tl,{PW_TW}1,,otpauth://totp/x:alice?secret={SEED}&issuer=x\n"
                  f"tl,https://tl2.example/,bob-tl,{PW_TW}2,,otpauth://totp/x:bob?secret={SEED}&issuer=x\n")
    tl2 = csv_file("tl2.csv", "Title,URL,Username,Password,Notes,OTPAuth\n"
                   f"tl,https://tl1.example/,alice-tl,{PW_TW}1,,otpauth://totp/x:alice?secret={SEED2}&issuer=x\n"
                   f"tl,https://tl2.example/,bob-tl,{PW_TW}2,,otpauth://totp/x:bob?secret={SEED3}&issuer=x\n")
    rc1, o1 = tty([BIN, "import-passwords", "--csv", tl], [("Vault passphrase", PASS + "\n")])
    rc2, o2 = tty([BIN, "test-legacy-totp-link", "tl1.example-otp", "tl1.example", "--no-id"], [("Vault passphrase", PASS + "\n")])
    rc3, o3 = tty([BIN, "test-legacy-totp-link", "tl2.example-otp", "tl2.example", "--no-id"], [("Vault passphrase", PASS + "\n")])
    rc4, o4 = tty([BIN, "import-passwords", "--csv", tl2], [("Vault passphrase", PASS + "\n")])
    s4 = secrets()
    recs = [json.loads(l) for l in open(alog)]
    check("P2: two legacy logins (no ids) with codes tied by name: one id each, persisted once, and the import that opened the vault "
          "replaces both codes (no new ones)", rc1 == rc2 == rc3 == rc4 == 0
          and "tl1.example-otp: tied to the login tl1.example by its id now" in o4 and "tl2.example-otp: tied to the login tl2.example by its id now" in o4
          and not any(n in s4 for n in ("tl1.example-otp-2", "tl2.example-otp-2"))
          and all(any(x.get("op") == "rotate" and x.get("name") == n and x.get("kind") == "totp" for x in recs)
                  for n in ("tl1.example-otp", "tl2.example-otp")), (o4[-500:], sorted(n for n in s4 if n.startswith("tl"))))
    bad = csv_file("bad.csv", "a,b\n1,2\n")
    rc, out = tty([BIN, "import-passwords", "--csv", bad], [("Vault passphrase", PASS + "\n")])
    check("a CSV without url and password columns is refused", rc != 0 and "no url and password columns" in out, out)

    # --- import-session: a site's cookies ------------------------------------------------------------------
    r = client("import-session", "--browser", "chrome", "--site", "github.com", "--name", "gh", "--vm", "ivm", "--to-profile", "work")
    check("import-session without a terminal: refused", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--name", "gh", "--vm", "ivm", "--to-profile", "work"], [("", None)])
    check("import-session needs at least one --site (no 'every site')", rc == 64, out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "github.com", "--name", "gh", "--vm", "ivm", "--to-profile",
                   "work", "--dry-run"], [("Dry run", None)])
    check("import-session --dry-run: cookie counts per host, says plainly that localStorage isn't imported, no values",
          rc == 0 and "github.com  1 cookie" in out and ".github.com  1 cookie" in out and "localStorage" in out
          and "not imported" in out and COOKIE_GH not in out and "Vault passphrase" not in out, out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "github.com", "--name", "gh", "--vm", "ivm", "--to-profile",
                   "work"], [("Vault passphrase", PASS + "\n")])
    sealed = os.path.join(vdir, "sessions", "gh.sealed")
    check("import-session: the site's cookies (only its own) sealed as a session for a VM profile", rc == 0 and "Sealed 2 cookies" in out
          and os.path.exists(sealed) and not any(v.encode() in open(sealed, "rb").read() for v in (COOKIE_GH, "user_session"))
          and not any(v in out for v in VALUES), out)
    lj = json.loads(client("list", "--json").stdout or "{}")
    gh = next((x for x in lj.get("sessions", []) if x["name"] == "gh"), {})
    check("list: the session, kind cookies, its sites and source, approval each", gh.get("kind") == "cookies" and gh.get("sites") == ["github.com"]
          and gh.get("source") == "chrome:Default" and gh.get("approval") == "each" and gh.get("profile") == "work", gh)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--profile", "Profile 3", "--site", "github.com", "--name", "old",
                   "--vm", "ivm", "--to-profile", "work"], [("Vault passphrase", PASS + "\n")])
    check("a cookie store older than version 24 (values without their domain) is refused, nothing sealed", rc != 0
          and "version 24" in out and not os.path.exists(os.path.join(vdir, "sessions", "old.sealed")), out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--profile", "Profile 4", "--site", "github.com", "--name", "relabeled",
                   "--vm", "ivm", "--to-profile", "work"], [("Vault passphrase", PASS + "\n")])
    check("a cookie relabeled from .google.com to github.com (its encrypted value names another domain) stops the import, nothing sealed",
          rc != 0 and "another domain" in out and not os.path.exists(os.path.join(vdir, "sessions", "relabeled.sealed")), out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "google.com", "--name", "g", "--vm", "ivm", "--to-profile", "work"],
                  [("Vault passphrase", PASS + "\n")])
    check("a site whose cookies reach an identity provider (google.com) needs --allow-identity-provider", rc != 0
          and "identity provider" in out and "--allow-identity-provider" in out and not os.path.exists(os.path.join(vdir, "sessions", "g.sealed")), out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "google.com", "--name", "g", "--vm", "ivm", "--to-profile", "work",
                   "--allow-identity-provider"], [("Vault passphrase", PASS + "\n")])
    check("...with it, sealed and marked: every load asks", rc == 0 and "always" in out, out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "localhost", "--name", "local", "--vm", "ivm", "--to-profile", "work"],
                  [("Vault passphrase", PASS + "\n")])
    check("import-session for a local test site", rc == 0, out)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "github.com", "--name", "gh", "--vm", "ivm", "--to-profile", "work"],
                  [("Vault passphrase", PASS + "\n")])
    check("a session name that exists needs --replace", rc != 0 and "--replace" in out, out)

    # loading: asks, then the VM's loader gets each value encrypted with the VM's Chrome key
    n = len(calls())
    proc = background("session", "load", "gh", "--vm", "ivm")
    u.expect("the cookies imported for github.com")
    u.expect("Type gh to allow")
    u.send("gh\n")
    rc, out, _ = finish(proc)
    got = json.load(open(os.path.join(stubdir, "loaded-cookies.json"))) if os.path.exists(os.path.join(stubdir, "loaded-cookies.json")) else {}
    load_cmd = calls()[-1]["argv"][-1] if len(calls()) > n else ""
    ok_enc = True
    for c in got.get("cookies", []):
        plain = cbc(LINUX_KEY, bytes.fromhex(c["enc"])[3:], decrypt=True)
        plain_h = cbc(LINUX_KEY, bytes.fromhex(c["enc_hashed"])[3:], decrypt=True)
        want = {"user_session": COOKIE_GH, "_gh_sess": COOKIE_GH + "2"}.get(c["name"])
        ok_enc &= plain.decode() == want and plain_h == hashlib.sha256(c["host_key"].encode()).digest() + want.encode() and "value" not in c
    check("session load of imported cookies: asks, runs the cookie loader as the desktop user, each value encrypted with the VM's key "
          "(with and without the domain hash), no plaintext value sent", rc == 0 and calls()[-1]["kind"] == "cookies:load"
          and calls()[-1]["user"] == "cua" and "mcomp-cookie-load" in load_cmd and len(got.get("cookies", [])) == 2 and ok_enc, (out, got))
    rc, out = tty([BIN, "session", "policy", "g", "--approval", "session"], [("Vault passphrase", PASS + "\n")])
    proc = background("session", "load", "g", "--vm", "ivm")
    u.expect("Type g to allow")
    u.send("no\n")
    rc2, out2, _ = finish(proc)
    check("an identity provider's session asks at every load, even with policy 'session'", rc == 0 and rc2 == 2, (out, out2))
    rc, out = tty([BIN, "session", "policy", "local", "--approval", "session"], [("Vault passphrase", PASS + "\n")])
    r = client("session", "load", "local", "--vm", "ivm")
    check("an ordinary imported session with policy 'session' loads into its VM without asking", r.returncode == 0, r.stdout + r.stderr)
    open(os.path.join(stubdir, "load-leak-next"), "w").close()
    r = client("session", "load", "local", "--vm", "ivm")
    last = json.loads(open(alog).read().splitlines()[-1])
    check("a cookie load that fails after the VM got the cookies reports the vault's own words, never the VM's error output",
          r.returncode != 0 and LEAK not in r.stdout + r.stderr and "open it once in page-mode Chrome" in r.stdout + r.stderr
          and LEAK not in open(alog).read() and last.get("op") == "session_load" and last.get("result") != "ok", (r.stdout, r.stderr))

    # --site: only the cookies those sites get, filtered on the Mac
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "github.com", "--site", "localhost", "--name", "multi",
                   "--vm", "ivm", "--to-profile", "work"], [("Vault passphrase", PASS + "\n")])
    check("import-session for two sites", rc == 0 and "Sealed 3 cookies" in out, out)
    proc = background("session", "load", "multi", "--vm", "ivm", "--site", "localhost")
    try:
        u.expect("only the cookies for localhost", timeout=10)
        u.expect("Type multi to allow")
        u.send("multi\n")
    except AssertionError:
        pass
    rc, out, _ = finish(proc)
    got2 = json.load(open(os.path.join(stubdir, "loaded-cookies.json")))
    last = json.loads(open(alog).read().splitlines()[-1])
    check("session load --site localhost: the question names that scope, and only localhost's cookie goes to the VM (and the audit says so)",
          rc == 0 and [c["host_key"] for c in got2.get("cookies", [])] == ["localhost"] and js(out).get("cookies") == 1
          and last.get("op") == "session_load" and last.get("sites") == ["localhost"], (out, got2, last))
    r = client("session", "load", "multi", "--vm", "ivm", "--site", "bank.example")
    check("--site outside what the session holds is refused before anything is sent, without repeating the caller's site",
          r.returncode == 2 and "only, not every site asked for" in r.stdout + r.stderr and "bank.example" not in r.stdout,
          r.stdout + r.stderr)

    # replacing keeps an identity provider's mark
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "localhost", "--name", "g", "--vm", "ivm", "--to-profile", "work",
                   "--replace"], [("Vault passphrase", PASS + "\n")])
    lj = json.loads(client("list", "--json").stdout or "{}")
    g2 = next((x for x in lj.get("sessions", []) if x["name"] == "g"), {})
    check("replacing an identity provider's session keeps its mark (and approval each)", rc == 0 and g2.get("identity_provider") is True
          and g2.get("approval") == "each", (out, g2))

    # the loader itself, against a VM-side profile made here
    loader = shlex.split(load_cmd[load_cmd.index("exec python3"):])[4] if "exec python3" in load_cmd else ""
    vroot = os.path.join(td, "vmroot")
    vdb = os.path.join(vroot, "work", "Default", "Network", "Cookies")
    make_cookies(vdb, [("github.com", "user_session", "old-dummy-value"), ("keep.example", "k", "Dummy-keep")], key=LINUX_KEY)

    def run_loader(data, profile="work"):
        return subprocess.run([sys.executable, "-I", "-c", loader, vroot, profile], input=json.dumps(data).encode(), capture_output=True, timeout=60)

    r = run_loader(got)
    db = sqlite3.connect(vdb)
    rows = {(h, nme): v for h, nme, v in db.execute("SELECT host_key, name, encrypted_value FROM cookies")}
    db.close()
    dec = {k: cbc(LINUX_KEY, v[3:], decrypt=True) for k, v in rows.items()}
    check("loader: the cookies go into the profile's own store, replacing the same host/name/path, keeping the others",
          r.returncode == 0 and json.loads(r.stdout).get("cookies") == 2
          and dec.get(("github.com", "user_session")) == hashlib.sha256(b"github.com").digest() + COOKIE_GH.encode()
          and ("keep.example", "k") in dec and len(rows) == 3, (r.stderr, list(rows)))
    r = run_loader(got, profile="missing")
    check("loader: a profile the driver never opened is refused with what to do (exit 6)", r.returncode == 6 and b"open it once" in r.stderr, r.stderr)
    make_cookies(os.path.join(vroot, "keyring", "Default", "Network", "Cookies"), [("a.example", "x", "y")], key=LINUX_KEY)
    kdb = sqlite3.connect(os.path.join(vroot, "keyring", "Default", "Network", "Cookies"))
    kdb.execute("UPDATE cookies SET encrypted_value = ?", (b"v11" + b"\x00" * 32,))
    kdb.commit()
    kdb.close()
    r = run_loader(got, profile="keyring")
    check("loader: a store encrypted with a keyring is refused", r.returncode == 5 and b"keyring" in r.stderr, r.stderr)
    os.symlink(os.path.join(vroot, "work"), os.path.join(vroot, "linked"))
    r = run_loader(got, profile="linked")
    check("loader: a profile folder that is a symbolic link is refused", r.returncode == 5 and b"symbolic link" in r.stderr, r.stderr)

    u.send("\x03")
    u.wait(15)

    # --- the audit log and every output -------------------------------------------------------------------
    text = open(alog).read()
    recs = [json.loads(l) for l in text.splitlines()]
    check("audit: each imported login (set/rotate with imported_from and a masked username), each import, each sealed session",
          any(x["op"] == "set" and x.get("imported_from", "").startswith("Chrome") and x.get("username_hint") for x in recs)
          and any(x["op"] == "rotate" and x.get("name") == "github.com" for x in recs)
          and sum(1 for x in recs if x["op"] == "import_passwords") >= 6
          and any(x["op"] == "import_session" and x.get("identity_provider") is True for x in recs), [x["op"] for x in recs][-20:])
    check("the audit log holds no values and no unmasked username", not any(v in text for v in VALUES + ["a.person@example.com", "dummy-user-qx7"]))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in the output of any of {len(outputs)} commands", not leaked, leaked)


if __name__ == "__main__":
    sys.exit(main())
