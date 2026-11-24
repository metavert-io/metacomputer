"""The user's hands in the live view, for the VM tests: a small VNC (RFB 3.8) client that connects to a test VM's live
view the way Screen Sharing does (127.0.0.1:VNC_PORT on this Mac, the gateway's forward to the VM's x11vnc, with the
live-view password) and sends pointer and key events, nothing else. x11vnc turns them into XTest events on the X
server's core pointer and keyboard, so this is the same path a person's clicks and keys take; it never reads the
screen. Stdlib only: VNC authentication's DES is here too (encryption of one block, as RFB uses it).

    with LiveView(port, password) as v:
        v.click(x, y)            # screen pixels of the VM's desktop
        v.type("text")           # ASCII letters, digits and punctuation, as keysyms
        v.key("Return")          # or "Tab", "Escape", "BackSpace", "ctrl+t"

The password is passed in by the caller (read from the test VM's own file) and kept only in this process: never in an
argument, output or log.
"""
import socket
import struct
import time

# --- DES (FIPS 46-3), encryption only ----------------------------------------------------------------------------------
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4,
      25]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]]


def _permute(v, table, width):
    """The bits of v (width bits, bit 1 = the most significant) in the order table names."""
    out = 0
    for pos in table:
        out = (out << 1) | ((v >> (width - pos)) & 1)
    return out


def _subkeys(key):
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d, keys = k >> 28, k & 0xFFFFFFF, []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        keys.append(_permute((c << 28) | d, _PC2, 56))
    return keys


def _f(r, k):
    x = _permute(r, _E, 32) ^ k
    out = 0
    for i in range(8):
        six = (x >> (42 - 6 * i)) & 0x3F
        out = (out << 4) | _S[i][(((six & 0x20) >> 4) | (six & 1)) * 16 + ((six >> 1) & 0xF)]
    return _permute(out, _P, 32)


def des_encrypt(key, block):
    """One 8-byte block under an 8-byte key."""
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for k in _subkeys(key):
        left, right = right, left ^ _f(right, k)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def vnc_response(password, challenge):
    """VNC authentication: the challenge's two blocks encrypted under the password's first 8 bytes, each byte's bits
    reversed (RFB 3.8, 7.2.2)."""
    pw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in pw)
    return des_encrypt(key, challenge[:8]) + des_encrypt(key, challenge[8:16])


# --- the client --------------------------------------------------------------------------------------------------------
KEYSYMS = {"Return": 0xFF0D, "Tab": 0xFF09, "Escape": 0xFF1B, "BackSpace": 0xFF08, "ctrl": 0xFFE3, "shift": 0xFFE1,
           "alt": 0xFFE9, "Left": 0xFF51, "Right": 0xFF53, "Up": 0xFF52, "Down": 0xFF54, "space": 0x20}


class VNCError(Exception):
    pass


class LiveView:
    def __init__(self, port, password, host="127.0.0.1", timeout=15.0):
        self.addr, self.password, self.timeout = (host, int(port)), password, timeout
        self.sock, self.size = None, None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VNCError("the live view closed the connection")
            buf += chunk
        return buf

    def connect(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        version = self._recv(12)
        if not version.startswith(b"RFB 003."):
            raise VNCError("not a VNC server")
        self.sock.sendall(b"RFB 003.008\n")
        n = self._recv(1)[0]
        if n == 0:
            raise VNCError("the live view refused the connection")
        types = self._recv(n)
        if 2 in types:
            self.sock.sendall(b"\x02")
            self.sock.sendall(vnc_response(self.password, self._recv(16)))
        elif 1 in types:
            self.sock.sendall(b"\x01")
        else:
            raise VNCError(f"no security type this client speaks ({list(types)})")
        if struct.unpack(">I", self._recv(4))[0] != 0:
            raise VNCError("the live view refused the password")
        self.sock.sendall(b"\x01")                              # ClientInit: shared, as Screen Sharing connects
        w, h = struct.unpack(">HH", self._recv(4))
        self._recv(16)                                          # pixel format
        self._recv(struct.unpack(">I", self._recv(4))[0])       # the desktop's name
        self.size = (w, h)
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def pointer(self, x, y, buttons=0):
        self.sock.sendall(struct.pack(">BBHH", 5, buttons, int(x), int(y)))

    def click(self, x, y, pause=0.12):
        self.pointer(x, y)
        time.sleep(pause)
        self.pointer(x, y, 1)
        time.sleep(0.06)
        self.pointer(x, y, 0)
        time.sleep(pause)

    def keysym(self, sym, down):
        self.sock.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))

    def key(self, name, pause=0.05):
        """One key or a chord ("ctrl+t")."""
        syms = [KEYSYMS.get(p, ord(p) if len(p) == 1 else None) for p in name.split("+")]
        if None in syms:
            raise VNCError(f"no keysym for {name!r}")
        for s in syms:
            self.keysym(s, True)
            time.sleep(0.02)
        for s in reversed(syms):
            self.keysym(s, False)
            time.sleep(0.02)
        time.sleep(pause)

    def type(self, text, pause=0.04):
        """ASCII text (Latin-1 keysyms are the code points); x11vnc adds Shift where the keymap needs it."""
        for ch in text:
            if not 0x20 <= ord(ch) < 0x7F:
                raise VNCError("type() sends ASCII only")
            self.keysym(ord(ch), True)
            self.keysym(ord(ch), False)
            time.sleep(pause)
