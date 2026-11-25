"""The sessions of the agents that keep them in SQLite databases (each one's coding-agents/<name>/sessions.py says
where and what: KIND, databases, read), read for `metacomputer learn` (docs/learn.md; lib/transcripts.py reads the
agents that keep files) and turned into lib/transcripts.py's Transcript, so the same mining applies.

How: never through the agent, and never a write or a lock on its files. A database is read into memory as it was at its
last commit: its main file plus the commits in its write-ahead log (the -wal file, where a running agent keeps its
newest sessions), whose frames are checked as SQLite checks them (the log's salts, the running checksums, commit
frames) and applied to the copy in memory, which SQLite then opens as an in-memory database (deserialize). Its files are
only read: opened O_RDONLY, no -shm, no lock, one folder at a time from the agent's own folder (its state folder or
home, which may itself be a link the account made) without following a link below it, each folder and file this
account's (a database another account owns, or reached through a link, is refused and noted). A database larger than
MAX_SNAPSHOT (or a log larger than MAX_WAL, or a log whose commits would make it larger) is opened in place with
immutable=1 through the descriptor already opened and checked (/dev/fd/N: never by its path again; no lock, no write;
the latest commits still in its log are then left out, and a note says so). Every connection keeps SQLite's temporary
data in memory (temp_store, so a sort never spills a session into a file) and is interrupted at its deadline (a
progress handler, so one query can't run past it). A layout this module doesn't know, a database that can't be read
and compressed rows it can't decode are counted in notes (fixed words, an agent's name and counts: never a path or a
value from the database), never fatal. Bounded like lib/transcripts.py: sessions (in the query itself), rows, calls
and time per database. Stdlib only.
"""
import errno
import json
import os
import re
import sqlite3
import stat
import struct
import time
import urllib.parse

import transcripts
import wirelimits



MAX_SNAPSHOT = 256 << 20        # main file + log read into memory at most this; larger: opened in place, immutable
MAX_WAL = 128 << 20             # a larger log isn't applied (its checksums are computed here, in Python)
MAX_SESSIONS = 2000             # recent sessions read from one database
MAX_ROW = 8 << 20               # one row's JSON at most (a larger one is skipped, counted)
MAX_SECONDS = 120.0             # time spent on one database
MAX_AGENT_DIRS = 200            # OpenClaw agents (and Hermes profiles) looked at per state folder
WAL_MAGIC = (0x377F0682, 0x377F0683)


class Unreadable(Exception):
    """A database (or its log) this can't read: why, in fixed words."""


class Place(str):
    """A database's path, knowing the agent's own folder it's in (root): open_db opens it from there down."""
    root = None


def _place(root, *parts):
    p = Place(os.path.join(root, *parts))
    p.root = root
    return p


class Layout(Exception):
    """A database whose tables don't have what this reads."""


# --- reading a database without a write or a lock -------------------------------------------------------------
LINKED = "it, or a folder on its way, is a link"
OTHERS = "it, or a folder on its way, belongs to another account"
SLOW = "a database took too long to read, so some of its sessions were left out"


def _me():
    return os.getuid()


def _why(e, dfd, name):
    """Fixed words for an open that failed: a link (O_NOFOLLOW's ELOOP, or ENOTDIR with O_DIRECTORY), not there."""
    if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                return LINKED
        except OSError:
            pass
    return "isn't there" if e.errno in (errno.ENOENT, errno.ENOTDIR) else "can't be opened"


def _mine(fd):
    """fd's stat; raises Unreadable when another account owns it."""
    st = os.fstat(fd)
    if st.st_uid != _me():
        raise Unreadable(OTHERS)
    return st


def _folder(path, root):
    """(descriptor of the folder holding the database, the database's name): the agent's own folder (root) opened as
    the account has it (it may be a link the account made), then each folder below it opened from the one above without
    following a link (O_NOFOLLOW, openat-style), every one of them this account's. Raises Unreadable."""
    root = os.path.abspath(root)
    rel = os.path.relpath(os.path.abspath(path), root)
    parts = rel.split(os.sep)
    if any(x in ("", ".", "..") for x in parts):
        raise Unreadable("isn't in its agent's folder")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        raise Unreadable("isn't there") from None
    except OSError:
        raise Unreadable("can't be opened") from None
    try:
        _mine(fd)
        for name in parts[:-1]:
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                raise Unreadable(_why(e, fd, name)) from None
            os.close(fd)
            fd = nfd
            _mine(fd)
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _open_in(dfd, name):
    """A descriptor of the regular file name in the folder dfd, opened O_RDONLY without following a link and checked
    (a regular file, this account's), or None when it isn't there. Raises Unreadable."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise Unreadable(_why(e, dfd, name)) from None
    try:
        if not stat.S_ISREG(_mine(fd).st_mode):
            raise Unreadable("isn't a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(dfd, name, cap):
    """(bytes, identity) of the regular file name in the folder dfd (_open_in) read whole, or None when it isn't there;
    raises Unreadable when it's larger than cap or can't be read."""
    fd = _open_in(dfd, name)
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if st.st_size > cap:
            raise Unreadable("too large")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            total += len(b)
            if total > cap:
                raise Unreadable("too large")
            chunks.append(b)
        return b"".join(chunks), (st.st_dev, st.st_ino)
    except OSError:
        raise Unreadable("can't be read") from None
    finally:
        os.close(fd)


def _read_head(dfd, name, n):
    """The first n bytes of the regular file name in the folder dfd, or None when it isn't there (or can't be read)."""
    try:
        fd = _open_in(dfd, name)
    except Unreadable:
        return None
    if fd is None:
        return None
    try:
        return os.read(fd, n)
    except OSError:
        return None
    finally:
        os.close(fd)


def _guard(con, deadline):
    """Every connection: no write (query_only), SQLite's temporary data (sorts, temporary indexes) in memory, never in a
    file (temp_store), and every query interrupted once the deadline has passed (a progress handler)."""
    con.execute("PRAGMA query_only = ON")
    con.execute("PRAGMA temp_store = MEMORY")
    if deadline is not None:
        con.set_progress_handler(lambda: time.monotonic() > deadline, 2000)


def _interrupted(e):
    return isinstance(e, sqlite3.OperationalError) and "interrupt" in str(e)


def _cksum(data, s1, s2, big):
    """SQLite's WAL checksum (wal.c walChecksumBytes) over data, continuing from (s1, s2)."""
    n = len(data) // 4
    w = struct.unpack((">" if big else "<") + f"{n}I", data[:n * 4])
    m = 0xFFFFFFFF
    for i in range(0, n - 1, 2):
        s1 = (s1 + w[i] + s2) & m
        s2 = (s2 + w[i + 1] + s1) & m
    return s1, s2


def apply_wal(db, wal, deadline=None):
    """The database image (bytearray db) with the committed transactions of its write-ahead log applied, as SQLite's
    recovery reads a log: a header with a known magic and a valid checksum, then frames while each carries the header's
    salts and a matching running checksum; a frame's page is applied only when a commit frame (a nonzero database size)
    ends its transaction, and the image takes that size. Returns (db, commits applied); raises Unreadable for a log that
    doesn't fit the database (another page size), and "too large" before a commit would make the image larger than
    MAX_SNAPSHOT (nothing that size is allocated)."""
    if len(wal) < 32:
        return db, 0
    magic, version, psize, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    if magic not in WAL_MAGIC or version != 3007000:
        return db, 0                                  # SQLite ignores a log with another header
    big = bool(magic & 1)
    if psize < 512 or psize > 65536 or psize & (psize - 1):
        return db, 0
    if _cksum(wal[:24], 0, 0, big) != (c1, c2):
        return db, 0
    if len(db) >= 100:
        dbp = int.from_bytes(db[16:18], "big")
        if (65536 if dbp == 1 else dbp) != psize:
            raise Unreadable("its write-ahead log doesn't match it")
    s, off, fsize, pending, commits = (c1, c2), 32, 24 + psize, {}, 0
    while off + fsize <= len(wal):
        if deadline is not None and commits % 64 == 0 and time.monotonic() > deadline:
            raise Unreadable("took too long")
        pg, size, fs1, fs2, fc1, fc2 = struct.unpack(">6I", wal[off:off + 24])
        if (fs1, fs2) != (salt1, salt2) or pg == 0:
            break
        s = _cksum(wal[off:off + 8], s[0], s[1], big)
        s = _cksum(wal[off + 24:off + fsize], s[0], s[1], big)
        if s != (fc1, fc2):
            break
        pending[pg] = off + 24
        if size:
            end = size * psize
            if end > MAX_SNAPSHOT:
                raise Unreadable("too large")
            if len(db) < end:
                db.extend(bytes(end - len(db)))
            for p, o in pending.items():
                if p <= size:
                    db[(p - 1) * psize:p * psize] = wal[o:o + psize]
            del db[end:]
            pending, commits = {}, commits + 1
        off += fsize
    return db, commits


def _snapshot(dfd, name, deadline):
    """The database as of its last commit (main file + its log's commits), opened in memory; raises Unreadable."""
    for _ in range(3):
        head = _read_head(dfd, name + "-wal", 32)
        main = _read_file(dfd, name, MAX_SNAPSHOT)
        if main is None:
            raise Unreadable("isn't there")
        wal = _read_file(dfd, name + "-wal", MAX_WAL)
        if (head or b"") == (wal[0][:32] if wal is not None else b""):
            break                                     # the log wasn't restarted while the main file was read
    else:
        raise Unreadable("kept changing while it was read")
    db = bytearray(main[0])
    if wal is not None:
        db, _ = apply_wal(db, wal[0], deadline)
    if len(db) < 100 or not db.startswith(b"SQLite format 3\0"):
        raise Unreadable("isn't a SQLite database")
    if db[18] == 2 or db[19] == 2:                    # an in-memory copy can't be in WAL mode: as a rollback-journal one
        db[18] = db[19] = 1
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(bytes(db))
        _guard(con, deadline)
    except (sqlite3.Error, AttributeError, OverflowError):
        con.close()
        raise Unreadable("isn't a SQLite database this can open") from None
    return con


def _in_place(dfd, name, deadline):
    """The database opened where it is, immutable (SQLite takes no lock and writes nothing; it reads the main file
    only), through the descriptor _open_in opened and checked: /dev/fd/N, which macOS opens as that same open file, so
    the path is never looked up again (a file swapped for a link meanwhile isn't what's read)."""
    fd = _open_in(dfd, name)
    if fd is None:
        raise Unreadable("isn't there")
    try:
        con = sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro&immutable=1", uri=True)
        try:
            _guard(con, deadline)
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error:
            con.close()
            raise
    except sqlite3.Error:
        raise Unreadable("can't be opened") from None
    finally:
        os.close(fd)                                  # SQLite has its own descriptor of the same open file
    return con


def open_db(path, deadline=None, root=None):
    """(connection, note): the database read without a write or a lock, opened from the agent's own folder down (root:
    the Place's, else the database's own folder; _folder). note is None, or says the newest commits in its log were
    left out (a database or log too large to read into memory). Raises Unreadable."""
    root = root or getattr(path, "root", None) or os.path.dirname(os.path.abspath(path))
    dfd, name = _folder(path, root)
    try:
        fd = _open_in(dfd, name)
        if fd is None:
            raise Unreadable("isn't there")
        try:
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        wfd, wsize = _open_in(dfd, name + "-wal"), 0
        if wfd is not None:
            try:
                wsize = os.fstat(wfd).st_size
            finally:
                os.close(wfd)
        if size <= MAX_SNAPSHOT and wsize <= MAX_WAL:
            try:
                return _snapshot(dfd, name, deadline), None
            except Unreadable as e:
                if str(e) != "too large":             # grew past the cap while it was read: in place, below
                    raise
        return _in_place(dfd, name, deadline), ("its newest changes, still in its write-ahead log, were left out (too "
                                                "large to read into memory)" if wsize else None)
    finally:
        os.close(dfd)


def _columns(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as e:
        if _interrupted(e):
            raise
        return set()


# --- where they are -------------------------------------------------------------------------------------------
def _home(env):
    return env.get("HOME") or os.path.expanduser("~")


def _distinct(paths):
    seen, out = set(), []
    for p in paths:
        k = os.path.normpath(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _listdir(folder):
    try:
        return sorted(os.listdir(folder))[:MAX_AGENT_DIRS * 5]
    except OSError:
        return []


# --- turning rows into calls ----------------------------------------------------------------------------------
def _new(path, kind, sid, cwd):
    tr = transcripts.Transcript(path)
    tr.kind, tr.session_id, tr.cwd, tr.info = kind, sid, cwd, {}
    return tr


def _see(tr, t):
    if t is not None:
        tr.first = t if tr.first is None else min(tr.first, t)
        tr.last = t if tr.last is None else max(tr.last, t)


def _call(tr, cid, name, inp, t, last_t, slim):
    """A call as lib/transcripts.py keeps it: an MCP tool of the metacomputer server as mcp__metacomputer__<tool>, the
    agent's shell as Bash."""
    drv = transcripts.mcp_tool(name)
    if name == "Bash":
        inp = {"command": transcripts._str((inp or {}).get("command"), 2000 if slim else 4096) or ""}
    elif slim:
        inp = transcripts.bounded(inp) if drv else {}
    return {"id": (cid or f"{tr.kind}-{len(tr.calls)}")[:200], "name": name[:200], "tool": drv, "input": inp,
            "ctx": "", "t_use": t, "t_prev": last_t, "error": None, "text": "", "images": 0, "t_result": None}


def _result(call, error, text, images, t, slim):
    keep = transcripts.TEXT_KEEP_WINDOWS if call["tool"] in ("list_windows", "launch_app") else transcripts.TEXT_KEEP
    call.update(error=bool(error), text="" if slim and call["name"] == "Bash" else (text or "")[:keep],
                images=images, t_result=t)


def _iso(s):
    return transcripts._ts({"timestamp": s}) if isinstance(s, str) else None


def _args(v):
    """Tool arguments as a dict: a dict, or a JSON string of one; {} otherwise."""
    if isinstance(v, str):
        try:
            v = wirelimits.loads(v[:MAX_ROW])
        except (ValueError, RecursionError):
            return {}
    return v if isinstance(v, dict) else {}


def _when(v):
    """Seconds for a stored time: OpenClaw's milliseconds, or seconds."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def _recent(con, kind, table, column, cutoff, notes):
    """The ids of a table's sessions whose last row (MAX(column), seconds or milliseconds as _when reads them) is at or
    after cutoff, newest first: at most MAX_SESSIONS, chosen and limited in the query itself."""
    q = (f"SELECT sid, w FROM (SELECT session_id AS sid, MAX({column}) AS m, CASE WHEN MAX({column}) > 1e11 "
         f"THEN MAX({column}) / 1000.0 ELSE MAX({column}) END AS w FROM {table} GROUP BY session_id) "
         f"WHERE typeof(sid) = 'text' AND typeof(m) IN ('integer', 'real') AND m > 0 AND w >= ? "
         f"ORDER BY w DESC LIMIT ?")
    rows = [(sid, w) for sid, w in con.execute(q, (cutoff, MAX_SESSIONS + 1))
            if isinstance(sid, str) and _when(w) is not None]
    if len(rows) > MAX_SESSIONS:
        notes.add(kind, f"only its {MAX_SESSIONS} most recent sessions in a database were read")
    return [sid for sid, _ in rows[:MAX_SESSIONS]]


# --- all of them ----------------------------------------------------------------------------------------------
class Notes:
    """What couldn't be read, per agent, in fixed words with counts: {agent: {words: count}}."""

    def __init__(self):
        self.by = {}

    def add(self, agent, words, n=1):
        d = self.by.setdefault(agent, {})
        d[words] = d.get(words, 0) + n

    def lines(self):
        return [f"{LABELS.get(a, a)}: {w}" + (f" ({n})" if n > 1 else "")
                for a, d in sorted(self.by.items()) for w, n in d.items()]


def _readers():
    """{kind: (where its databases are, how to read one)} of every agent that keeps its sessions in databases
    (coding-agents/<name>/sessions.py: KIND, databases, read), in the registry's order."""
    from agentkit import manifests, registry
    out = {}
    for n in registry.AGENTS:
        ses = manifests.manifest(n).get("sessions")
        if ses and ses["source"] == "databases":
            mod = registry.module(n, "sessions")
            out[mod.KIND] = (mod.databases, mod.read)
    return out


def _labels():
    """{kind: label} for the notes (an agent's name, by its sessions' kind)."""
    from agentkit import manifests, registry
    out = {}
    for n in registry.AGENTS:
        ses = manifests.manifest(n).get("sessions")
        for k in (ses or {}).get("kinds") or ():
            out[k] = manifests.label(n)
    return out


READERS = _readers()
LABELS = _labels()


def sessions(env, cutoff, slim=True):
    """(every recent session of OpenClaw's and Hermes Agent's as a Transcript, in no order; databases looked at;
    databases that couldn't be read; Notes). A database that fails is counted and noted, never fatal."""
    out, looked, bad, notes = [], 0, 0, Notes()
    for kind, (where, read) in READERS.items():
        for path in where(env):
            looked += 1
            deadline = time.monotonic() + MAX_SECONDS
            try:
                con, note = open_db(path, deadline)
            except Unreadable as e:
                bad += 1
                notes.add(kind, f"a session database couldn't be read ({e})")
                continue
            if note:
                notes.add(kind, note)
            try:
                for tr in read(con, path, cutoff, notes, deadline, slim):
                    out.append(tr)
            except Layout:
                bad += 1
                notes.add(kind, "a session database has a layout this version of metacomputer doesn't read")
            except sqlite3.OperationalError as e:
                if _interrupted(e):                   # its deadline passed inside a query (_guard's progress handler)
                    notes.add(kind, SLOW)
                else:
                    bad += 1
                    notes.add(kind, "a session database couldn't be read (it may have changed while it was read; "
                                    "try again)")
            except (sqlite3.Error, MemoryError, ValueError):
                bad += 1
                notes.add(kind, "a session database couldn't be read (it may have changed while it was read; try "
                                "again)")
            finally:
                con.close()
    return out, looked, bad, notes
