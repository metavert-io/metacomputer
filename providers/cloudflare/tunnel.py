#!/usr/bin/env python3
"""ssh's ProxyCommand for a computer on Cloudflare Containers (the `cloudflare` transport), and its identity check: it
asks the computer's broker (providers/cloudflare/broker.py), the one process that holds the computer's key, over the
broker's private socket. This program never gets a credential; it carries bytes.

    tunnel.py relay --sock PATH --worker NAME    one SSH connection: standard input to the container's sshd, its
                                                 answers to standard output (run by lib/proxywrap.py, or the vault)
    tunnel.py check --sock PATH --worker NAME    the computer's identity as its Worker says it now, as JSON {"id": ...}
                                                 (lib/proxywrap.py --check compares it with the computer's record)

Standard library only, and Python 3.9 (macOS's /usr/bin/python3 runs it when the vault stages its own copy): no import
from the checkout. A broker that isn't running is said plainly (metacomputer --vm NAME start starts one)."""
import json
import os
import re
import selectors
import socket
import sys

USAGE = "usage: tunnel.py relay|check --sock PATH --worker NAME"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?")
SOCK = re.compile(r"/[A-Za-z0-9._/-]{1,200}\.sock")


def connect(sock_path):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except OSError:
        pass
    c.settimeout(60)
    try:
        c.connect(sock_path)
    except OSError:
        c.close()
        return None
    return c


def request(c, req):
    c.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        d = c.recv(1)                 # byte by byte: nothing past the answer's line is taken from the socket
        if not d:
            break
        buf += d
        if len(buf) > 65536:
            break
    try:
        return json.loads(buf.decode())
    except ValueError:
        return None


def pump(c):
    """Standard input to the socket and the socket to standard output until both ends have closed."""
    sel = selectors.DefaultSelector()
    c.setblocking(False)
    sel.register(0, selectors.EVENT_READ, "in")
    sel.register(c, selectors.EVENT_READ, "sock")
    sock_open = True
    while sock_open:
        for key, _ in sel.select():
            if key.data == "in":
                d = os.read(0, 65536)
                if not d:
                    sel.unregister(0)
                    try:
                        c.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    c.setblocking(True)
                    c.sendall(d)
                    c.setblocking(False)
            else:
                try:
                    d = c.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    d = b""
                if not d:
                    sock_open = False
                    break
                view = memoryview(d)
                while view:
                    n = os.write(1, view)
                    view = view[n:]
    return 0


def main(argv):
    if len(argv) != 5 or argv[0] not in ("relay", "check") or argv[1] != "--sock" or argv[3] != "--worker" \
            or not SOCK.fullmatch(argv[2]) or not NAME.fullmatch(argv[4]):
        print(USAGE, file=sys.stderr)
        return 2
    op, sock_path, worker = argv[0], argv[2], argv[4]
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: tunnel.py: taken " + json.dumps({"flags": [], "values": ["--sock", "--worker"], "words": 1,
                                                            "max_words": 1}))
        sys.exit(99)        # parse only: nothing was done (never 0: a check run this way refuses)
    c = connect(sock_path)
    if c is None:
        print(f"metacomputer: the Cloudflare connection broker for the Worker {worker} isn't running (it holds the "
              "computer's key; `metacomputer --vm NAME start` starts it): no connection was made", file=sys.stderr)
        return 1
    ans = request(c, {"op": op, "worker": worker})
    if op == "check":
        c.close()
        if not isinstance(ans, dict) or "id" not in ans:
            print("metacomputer: the broker didn't answer", file=sys.stderr)
            return 1
        print(json.dumps({"id": ans["id"], "state": ans.get("state")}))
        return 0
    if not isinstance(ans, dict) or not ans.get("ok"):
        why = (ans or {}).get("why") if isinstance(ans, dict) else None
        print(f"metacomputer: {str(why or 'the broker refused the connection')[:300]}", file=sys.stderr)
        c.close()
        return 1
    try:
        return pump(c)
    except (BrokenPipeError, ConnectionResetError):
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
