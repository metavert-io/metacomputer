"""OpenClaw's and Hermes Agent's sessions, which they keep in SQLite databases, read for `metacomputer learn`
(docs/learn.md; lib/transcripts.py reads the agents that keep files) and turned into lib/transcripts.py's Transcript, so
the same mining applies.

Where and what (checked with OpenClaw 2026.11.7 and Hermes Agent 0.23.3: their sources, and sessions each made in a
throwaway home with a stand-in model and a stand-in metacomputer server):
- OpenClaw: <state>/agents/<agent>/agent/openclaw-agent.sqlite in each state folder: OPENCLAW_STATE_DIR when set, then
  ~/.openclaw and every ~/.openclaw-<profile> (OPENCLAW_HOME moves ~ for it). Table transcript_events (session_id, seq,
  created_at in ms, the event as event_json, or zstd-compressed as event_zstd with its size in event_utf8_bytes;
  message_role in newer versions): one event per row, in the order of seq. The first is {"type": "session", "id",
  "cwd", "timestamp"}; a message is {"type": "message", "timestamp", "message": {"role": "user" | "assistant" |
  "toolResult", ...}}, an assistant's content holding {"type": "toolCall", "id", "name", "arguments"} items and a
  toolResult {"toolCallId", "toolName", "content", "isError", "details"}. Its MCP tools are named <server>__<tool>
  (metacomputer__click). With tool search on (OpenClaw's default) the model calls them through dispatch_action
  {"id": "mcp:metacomputer:metacomputer__click" or the tool's name, "args": {...}}, and the result's details carry the
  tool and its own result ({"tool": {"mcp": {"serverName", "toolName"}}, "result": {"content"}}). exec is its shell.
  Compressed rows are read with Python 3.14's compression.zstd; with an older Python they're counted, not read.
- Hermes Agent: state.db in its home (HERMES_HOME, else ~/.hermes) and in each profile (<root>/profiles/<name>/, the
  root being ~/.hermes, or a HERMES_HOME outside it as Hermes Agent resolves it). Tables sessions (id, cwd) and messages
  (session_id, role, content, tool_calls, tool_call_id, tool_name, timestamp in seconds; ordered by id): an assistant
  row's tool_calls is the OpenAI list [{"id", "function": {"name", "arguments"}}], and a "tool" row holds a result,
  wrapped in <untrusted_tool_result source="..."> around {"result": ...} or {"error": ...}. Its MCP tools are named
  mcp__metacomputer__<tool>. With tool search on (its default) the model calls them through tool_call {"calls":
  [{"name", "arguments"}]}; Hermes Agent splits a batch into one call each before it keeps it, and the tool row names the
  tool itself. terminal is its shell.

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

try:                                    # Python 3.14+: OpenClaw's compressed rows
    from compression import zstd as _zstd
except ImportError:                     # older Pythons: those rows are counted, not read
    _zstd = None

MAX_SNAPSHOT = 256 << 20        # main file + log read into memory at most this; larger: opened in place, immutable
MAX_WAL = 128 << 20             # a larger log isn't applied (its checksums are computed here, in Python)
MAX_SESSIONS = 2000             # recent sessions read from one database
MAX_ROW = 8 << 20               # one row's JSON at most (a larger one is skipped, counted)
MAX_ZSTD = 4 << 20              # OpenClaw's own limit for a compressed row (expanded size)
MAX_SECONDS = 120.0             # time spent on one database
MAX_AGENT_DIRS = 200            # OpenClaw agents (and Hermes profiles) looked at per state folder
WAL_MAGIC = (0x377F0682, 0x377F0683)
UNTRUSTED_WRAP = re.compile(r'^<untrusted_tool_result source="[^"\n]{0,200}">\n', re.S)
LABELS = {"openclaw": "OpenClaw", "hermes": "Hermes Agent"}


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


def openclaw_dbs(env):
    """OpenClaw's session databases: <state>/agents/<agent>/agent/openclaw-agent.sqlite in OPENCLAW_STATE_DIR (when set),
    ~/.openclaw and each ~/.openclaw-<profile> (~ being OPENCLAW_HOME when set), as Places whose root is the state
    folder."""
    base = os.path.expanduser(env.get("OPENCLAW_HOME") or _home(env))
    states = [os.path.expanduser(env["OPENCLAW_STATE_DIR"])] if env.get("OPENCLAW_STATE_DIR") else []
    states += [os.path.join(base, ".openclaw")] + [os.path.join(base, n) for n in _listdir(base)
                                                    if n.startswith(".openclaw-")]
    out = []
    for s in _distinct(states):
        adir = os.path.join(s, "agents")
        for a in _listdir(adir)[:MAX_AGENT_DIRS]:
            p = _place(s, "agents", a, "agent", "openclaw-agent.sqlite")
            if os.path.lexists(p):
                out.append(p)
    return _distinct(out)


def hermes_dbs(env):
    """Hermes Agent's session databases: state.db in HERMES_HOME (when set), in its root (~/.hermes, or a HERMES_HOME
    outside it: that folder, or the folder above profiles/ when it is a profile) and in each profile under the root, as
    Places whose root is that home (a profile's: the root)."""
    native = os.path.join(_home(env), ".hermes")
    hh = os.path.abspath(os.path.expanduser(env["HERMES_HOME"])) if env.get("HERMES_HOME") else None
    root = native
    if hh and not (hh == native or hh.startswith(native.rstrip("/") + "/")):
        root = os.path.dirname(os.path.dirname(hh)) if os.path.basename(os.path.dirname(hh)) == "profiles" else hh
    places = [_place(h, "state.db") for h in ([hh] if hh else []) + [root]]
    places += [_place(root, "profiles", n, "state.db") for n in _listdir(os.path.join(root, "profiles"))[:MAX_AGENT_DIRS]
               if not n.startswith(".")]
    return [p for p in _distinct(places) if os.path.lexists(p)]


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


def _content_text(content):
    """OpenClaw's result content ([{"type": "text", "text"}, {"type": "image", ...}] or a string): (text, images)."""
    if isinstance(content, str):
        return content[:transcripts.TEXT_KEEP_WINDOWS], 0
    texts, imgs, size = [], 0, 0
    for x in content if isinstance(content, list) else []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "image":
            imgs += 1
        elif x.get("type") == "text" and isinstance(x.get("text"), str) and size < transcripts.TEXT_KEEP_WINDOWS:
            texts.append(x["text"][:transcripts.TEXT_KEEP_WINDOWS])
            size += len(texts[-1])
    return "\n".join(texts), imgs


OC_BRIDGE = "dispatch_action"
OC_MCP_ID = "mcp:metacomputer:"


def _openclaw_name(name, args):
    """(name as lib/transcripts.py names it, input) for an OpenClaw tool call: metacomputer__<tool> (direct, or through
    dispatch_action) -> mcp__metacomputer__<tool>; exec -> Bash; anything else by its own name."""
    if name == OC_BRIDGE:
        target = args.get("id") if isinstance(args.get("id"), str) else args.get("name")
        target = target[len(OC_MCP_ID):] if isinstance(target, str) and target.startswith(OC_MCP_ID) else target
        if isinstance(target, str) and target.startswith("metacomputer__"):
            return "mcp__" + target[:200], args.get("args") if isinstance(args.get("args"), dict) else {}
        return name, {}
    if name.startswith("metacomputer__"):
        return "mcp__" + name, args
    if name == "exec":
        return "Bash", args
    return name, {}


def openclaw_session(path, sid, events, slim=True):
    """One OpenClaw session (its events, in order) as a Transcript."""
    tr = _new(path, "openclaw", sid, None)
    by_id, last_t = {}, None
    for ev in events:
        t = _iso(ev.get("timestamp"))
        _see(tr, t)
        if ev.get("type") == "session":
            tr.cwd = tr.cwd or transcripts._str(ev.get("cwd"))
            continue
        msg = ev.get("message") if ev.get("type") == "message" else None
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            last_t = t if t is not None else last_t
        elif role == "assistant" and isinstance(msg.get("content"), list):
            for c in msg["content"]:
                if not (isinstance(c, dict) and c.get("type") == "toolCall" and isinstance(c.get("name"), str)):
                    continue
                if len(tr.calls) >= transcripts.MAX_CALLS:
                    tr.info["truncated"] = "calls"
                    break
                name, inp = _openclaw_name(c["name"], _args(c.get("arguments")))
                call = _call(tr, transcripts._str(c.get("id"), 200), name, inp, t, last_t, slim)
                by_id[call["id"]] = call
                tr.calls.append(call)
                last_t = t if t is not None else last_t
        elif role == "toolResult":
            cid = msg.get("toolCallId")
            call = by_id.get(cid[:200]) if isinstance(cid, str) else None
            if call is None or call["error"] is not None:
                continue
            det = msg.get("details") if isinstance(msg.get("details"), dict) else {}
            inner = det.get("result") if isinstance(det.get("result"), dict) else None
            text, imgs = _content_text(inner.get("content") if inner is not None else msg.get("content"))
            _result(call, msg.get("isError") is True, text, imgs, t, slim)
            last_t = t if t is not None else last_t
    return tr


def _openclaw_rows(con, sid, cols, info, deadline):
    """The session's events, in order (compressed ones expanded when this Python can, and only those that can hold a
    tool call or its result when the table says which)."""
    has_z = {"event_zstd", "event_utf8_bytes"} <= cols
    has_role = "message_role" in cols
    q = ("SELECT seq, CASE WHEN length(event_json) > ? THEN NULL ELSE event_json END, length(event_json) > ?, "
         + ("event_zstd, event_utf8_bytes, " if has_z else "NULL, NULL, ")
         + ("message_role " if has_role else "NULL ")
         + "FROM transcript_events WHERE session_id = ? ORDER BY seq LIMIT ?")
    first = True
    for n, (seq, text, big, blob, size, role) in enumerate(con.execute(q, (MAX_ROW, MAX_ROW, sid,
                                                                             transcripts.MAX_EVENTS))):
        if n % 500 == 0 and time.monotonic() > deadline:
            info["truncated"] = "time"
            return
        wanted = first or not has_role or role in ("assistant", "toolResult", "user")
        first = False
        if big:
            info["skipped"] = info.get("skipped", 0) + 1
            continue
        if text is None and blob is not None:
            if not wanted:
                continue
            if _zstd is None:
                info["compressed"] = info.get("compressed", 0) + 1
                continue
            if not isinstance(size, int) or not 0 < size <= MAX_ZSTD or not isinstance(blob, bytes):
                info["skipped"] = info.get("skipped", 0) + 1
                continue
            try:
                raw = _zstd.ZstdDecompressor().decompress(blob, max_length=size + 1)
                text = raw.decode("utf-8") if len(raw) == size else None
            except (Exception,):            # a corrupt frame (zstd's own error type) is one skipped row
                text = None
            if text is None:
                info["skipped"] = info.get("skipped", 0) + 1
                continue
        if not isinstance(text, str):
            continue
        try:
            ev = wirelimits.loads(text)
        except (ValueError, RecursionError):
            info["skipped"] = info.get("skipped", 0) + 1
            continue
        if isinstance(ev, dict):
            yield ev


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


def read_openclaw(con, path, cutoff, notes, deadline, slim=True):
    cols = _columns(con, "transcript_events")
    if not {"session_id", "seq", "event_json", "created_at"} <= cols:
        raise Layout()
    for sid in _recent(con, "openclaw", "transcript_events", "created_at", cutoff, notes):
        if time.monotonic() > deadline:
            notes.add("openclaw", SLOW)
            return
        info = {}
        tr = openclaw_session(path, sid[:200], _openclaw_rows(con, sid, cols, info, deadline), slim)
        tr.info.update(info)
        if info.get("compressed"):
            notes.add("openclaw", "compressed entries weren't read (they need Python 3.14 or later)",
                      info["compressed"])
        yield tr


# --- Hermes Agent ---------------------------------------------------------------------------------------------
HE_BRIDGE = "tool_call"


def _hermes_result(content):
    """(error, text) of a Hermes Agent tool row: inside its <untrusted_tool_result> wrapper, the {"result"} or {"error"}
    JSON its tools answer with (an MCP tool's text, or its error)."""
    if not isinstance(content, str):
        return None, ""
    body = content
    m = UNTRUSTED_WRAP.match(body)
    if m:
        body = body[m.end():]
        if body.endswith("</untrusted_tool_result>"):
            body = body[:-len("</untrusted_tool_result>")]
        i = body.find("\n\n{")                         # after the wrapper's own notice
        body = body[i + 2:] if i >= 0 else body
    body = body.strip()
    try:
        d = wirelimits.loads(body[:MAX_ROW]) if body.startswith("{") else None
    except (ValueError, RecursionError):
        d = None
    if not isinstance(d, dict):
        return None, body[:transcripts.TEXT_KEEP_WINDOWS]
    if "error" in d:
        v = d["error"]
        return True, v if isinstance(v, str) else json.dumps(v)[:transcripts.TEXT_KEEP_WINDOWS]
    if "exit_code" in d and isinstance(d.get("exit_code"), int):         # its terminal tool
        return d["exit_code"] != 0, ""
    v = d.get("result", d)
    return False, (v if isinstance(v, str) else json.dumps(v))[:transcripts.TEXT_KEEP_WINDOWS]


def _hermes_calls(tc):
    """[(id, name, input)] of one OpenAI-style tool call in an assistant row: tool_call's entries one by one (a batch of
    more than one gets ids of their own, whose results can't be told apart), terminal as Bash."""
    if not isinstance(tc, dict):
        return []
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name, cid = fn.get("name"), tc.get("id") or tc.get("call_id")
    if not isinstance(name, str):
        return []
    cid = cid[:200] if isinstance(cid, str) else None
    args = _args(fn.get("arguments"))
    if name == HE_BRIDGE:
        entries = args.get("calls")
        if entries is None and isinstance(args.get("name"), str):
            entries = [{"name": args["name"], "arguments": args.get("arguments")}]
        if isinstance(entries, str):
            try:
                entries = wirelimits.loads(entries[:MAX_ROW])
            except (ValueError, RecursionError):
                entries = []
        entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
        out = []
        for k, e in enumerate(entries[:60]):
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                n = e["name"]
                out.append(((cid if len(entries) == 1 else f"{cid}#{k}") if cid else None,
                            "Bash" if n == "terminal" else n, _args(e.get("arguments"))))
        return out
    return [(cid, "Bash" if name == "terminal" else name, args)]


def hermes_session(path, sid, cwd, rows, slim=True):
    """One Hermes Agent session (its message rows, in order: role, content, tool_calls, tool_call_id, timestamp) as a
    Transcript."""
    tr = _new(path, "hermes", sid, cwd)
    by_id, last_t = {}, None
    for role, content, tool_calls, tcid, ts in rows:
        t = _when(ts)
        _see(tr, t)
        if role == "user":
            last_t = t if t is not None else last_t
        elif role == "assistant" and isinstance(tool_calls, str) and tool_calls.strip():
            try:
                tcs = wirelimits.loads(tool_calls[:MAX_ROW])
            except (ValueError, RecursionError):
                continue
            for tc in tcs if isinstance(tcs, list) else []:
                for cid, name, inp in _hermes_calls(tc):
                    if len(tr.calls) >= transcripts.MAX_CALLS:
                        tr.info["truncated"] = "calls"
                        break
                    call = _call(tr, cid, name, inp, t, last_t, slim)
                    if cid:
                        by_id[cid] = call
                    tr.calls.append(call)
            last_t = t if t is not None else last_t
        elif role == "tool" and isinstance(tcid, str):
            call = by_id.get(tcid[:200])
            if call is None or call["error"] is not None:
                continue
            err, text = _hermes_result(content)
            if err is None:
                err = False
            _result(call, err, text, 0, t, slim)
            last_t = t if t is not None else last_t
    return tr


def read_hermes(con, path, cutoff, notes, deadline, slim=True):
    mcols, scols = _columns(con, "messages"), _columns(con, "sessions")
    if not {"session_id", "role", "content", "tool_calls", "tool_call_id", "timestamp"} <= mcols:
        raise Layout()
    order = "id" if "id" in mcols else "rowid"
    named = "OR instr(coalesce(tool_name, ''), 'metacomputer') > 0" if "tool_name" in mcols else ""
    summary = "AND coalesce(_compressed_summary, 0) = 0" if "_compressed_summary" in mcols else ""
    for sid in _recent(con, "hermes", "messages", "timestamp", cutoff, notes):
        if time.monotonic() > deadline:
            notes.add("hermes", SLOW)
            return
        if not con.execute(f"SELECT 1 FROM messages WHERE session_id = ? AND (instr(coalesce(tool_calls, ''), "
                           f"'metacomputer') > 0 {named}) LIMIT 1", (sid,)).fetchone():
            continue
        cwd = None
        if {"id", "cwd"} <= scols:
            r = con.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
            cwd = transcripts._str(r[0]) if r else None
        rows = con.execute(
            f"SELECT role, CASE WHEN length(content) > ? THEN NULL ELSE content END, "
            f"CASE WHEN length(tool_calls) > ? THEN NULL ELSE tool_calls END, tool_call_id, timestamp FROM messages "
            f"WHERE session_id = ? {summary} ORDER BY {order} LIMIT ?", (MAX_ROW, MAX_ROW, sid, transcripts.MAX_EVENTS))
        shown = sid[len("local-"):] if sid.startswith("local-") else sid
        yield hermes_session(path, shown[:200], cwd, rows, slim)


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


READERS = {"openclaw": (openclaw_dbs, read_openclaw), "hermes": (hermes_dbs, read_hermes)}


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
