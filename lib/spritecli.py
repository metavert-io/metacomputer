"""The facade of Fly's `sprite` command for the Sprites adapter (lib/cloud/sprites.py): the Sprites API through
`sprite api` (the CLI adds its own sign-in, which it keeps in the login keychain: metacomputer never sees the token),
and `sprite exec` for the setup's first step. Only lib/remote.py makes one; the adapter takes it as a value, so tests
hand it a stand-in. Plain values in and out; nothing the command prints is passed on whole."""
import json
import os
import re
import selectors
import signal
import subprocess
import time

from cloud.adapter import AdapterError


class SpriteCLI:
    """The calls the adapter makes, through Fly's `sprite` command: plain values in and out, so a stand-in can take
    its place. The command runs with a small environment of its own (HOME and PATH: it reads its own sign-in), never
    the caller's, and nothing it prints is passed on whole."""

    MAX_STDOUT = 4 << 20        # an API answer or a command's output: far more than any of them should be
    MAX_STDERR = 64 << 10

    def __init__(self, path, timeout=120, max_stdout=None, max_stderr=None):
        self.path = path
        self.timeout = timeout
        self.max_stdout = max_stdout or self.MAX_STDOUT
        self.max_stderr = max_stderr or self.MAX_STDERR

    def env(self):
        return {"HOME": os.environ.get("HOME") or os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8"}

    def run(self, args, input=None, timeout=None, max_output=None):
        """The command's (exit status, stdout, stderr), read as it comes with a byte cap on each stream: what it prints
        can come from the Sprite (`exec`: a guest's output, a log a hostile program wrote to), so nothing is buffered
        past MAX_STDOUT (or max_output) and MAX_STDERR. Over the stdout cap, or past the timeout, the command (its whole
        process group) is ended and AdapterError says so; stderr past its cap is read and dropped."""
        cap = max_output or self.max_stdout
        limit = timeout or self.timeout
        try:
            p = subprocess.Popen([self.path, *args], stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), start_new_session=True)
        except FileNotFoundError:
            raise AdapterError(f"Fly's sprite command isn't at {self.path}") from None
        out, err, over = bytearray(), bytearray(), None
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ, "out")
        sel.register(p.stderr, selectors.EVENT_READ, "err")
        pending = memoryview(bytes(input)) if input is not None else None
        if pending is not None:
            os.set_blocking(p.stdin.fileno(), False)
            if len(pending):
                sel.register(p.stdin, selectors.EVENT_WRITE, "in")
            else:
                p.stdin.close()
        deadline = time.monotonic() + limit
        try:
            while len(sel.get_map()) and over is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    over = f"didn't finish in {limit} s"
                    break
                for key, _ in sel.select(min(left, 1.0)):
                    if key.data == "in":
                        try:
                            n = os.write(p.stdin.fileno(), pending[:65536])
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            n, pending = 0, pending[:0]
                        pending = pending[n:]
                        if not len(pending):
                            sel.unregister(p.stdin)
                            p.stdin.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    if key.data == "out":
                        out += chunk
                        if len(out) > cap:
                            over = f"printed more than {cap} bytes"
                            break
                    elif len(err) < self.max_stderr:
                        err += chunk[:self.max_stderr - len(err)]
        finally:
            sel.close()
            if over is not None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                try:
                    if f:
                        f.close()
                except OSError:
                    pass
        if over is not None:
            raise AdapterError(f"Fly's sprite command {over}; it was ended (nothing it printed is passed on)")
        return p.returncode, bytes(out), bytes(err)

    def api(self, org, path, method="GET", body=None, timeout=None):
        """(HTTP status, body text) of a Sprites API call, through `sprite api` (the CLI adds its own sign-in)."""
        args = ["-o", org, "api", path, "--", "-sS", "-X", method, "-w", "\n%{http_code}"]
        if body is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        rc, stdout, stderr = self.run(args, input=json.dumps(body).encode() if body is not None else None, timeout=timeout)
        out = stdout.decode(errors="replace")
        text, _, code = out.rstrip("\n").rpartition("\n")
        if rc != 0 or not code.isdigit():
            why = " ".join(stderr.decode(errors="replace").split())[-200:]
            raise AdapterError(f"Fly's Sprites API didn't answer ({why or 'no reason given'}); is the sprite command "
                               "signed in to that organization? (sprite login)")
        return int(code), text

    def _json(self, org, path, method="GET", body=None, ok=(200,), timeout=None):
        code, text = self.api(org, path, method, body, timeout)
        if code == 404:
            return None
        if code not in ok:
            raise AdapterError(f"Fly's Sprites API refused {method} {path.split('?')[0]}: HTTP {code}")
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise AdapterError(f"Fly's Sprites API answered {method} {path.split('?')[0]} with something that isn't "
                               "JSON") from None

    def info(self, org, name):
        d = self._json(org, f"/v1/sprites/{name}")
        return d if isinstance(d, dict) else None

    def list(self, org, prefix=None):
        d = self._json(org, "/v1/sprites" + (f"?prefix={prefix}" if prefix else ""))
        items = (d or {}).get("sprites") or (d or {}).get("data") or []
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)]

    def create(self, org, name, labels=()):
        body = {"name": name}
        if labels:
            body["labels"] = list(labels)
        d = self._json(org, "/v1/sprites", "POST", body, ok=(200, 201))
        if not isinstance(d, dict):
            raise AdapterError("Fly's Sprites API didn't make the Sprite")
        return d.get("data") if isinstance(d.get("data"), dict) else d

    def destroy(self, org, name):
        code, _ = self.api(org, f"/v1/sprites/{name}", "DELETE")
        if code not in (200, 202, 204, 404):
            raise AdapterError(f"Fly's Sprites API didn't delete the Sprite {name}: HTTP {code}")
        return code != 404

    def policy(self, org, name):
        d = self._json(org, f"/v1/sprites/{name}/policy/network")
        return (d or {}).get("rules") if isinstance(d, dict) else None

    def set_policy(self, org, name, rules):
        code, _ = self.api(org, f"/v1/sprites/{name}/policy/network", "POST", {"rules": rules})
        if code not in (200, 204):
            raise AdapterError(f"Fly's Sprites API didn't take the network policy: HTTP {code}")

    def checkpoints(self, org, name):
        d = self._json(org, f"/v1/sprites/{name}/checkpoints")
        return [c for c in (d if isinstance(d, list) else []) if isinstance(c, dict) and c.get("id") != "Current"]

    def checkpoint(self, org, name, comment):
        code, text = self.api(org, f"/v1/sprites/{name}/checkpoint", "POST", {"comment": comment}, timeout=600)
        m = re.search(r"Checkpoint (v[0-9]{1,9}) created", text)
        if code not in (200, 201) or not m:
            raise AdapterError(f"Fly didn't make the checkpoint (HTTP {code})")
        return m.group(1)

    def restore(self, org, name, version):
        code, text = self.api(org, f"/v1/sprites/{name}/checkpoints/{version}/restore", "POST", timeout=900)
        if code not in (200, 201, 202) or '"type":"error"' in text.replace(" ", ""):
            raise AdapterError(f"Fly didn't restore {version} (HTTP {code})")

    def delete_checkpoint(self, org, name, version):
        if not re.fullmatch(r"v[0-9]{1,9}", version):
            raise AdapterError(f"{version!r} isn't a checkpoint's version")
        code, _ = self.api(org, f"/v1/sprites/{name}/checkpoints/{version}", "DELETE")
        if code not in (200, 202, 204, 404):
            raise AdapterError(f"Fly didn't delete the checkpoint {version}: HTTP {code}")

    def exec(self, org, name, argv, input=None, timeout=None, max_output=None):
        """`sprite exec` (no port forwards back to this Mac): returns (exit status, stdout bytes, stderr bytes), each
        capped (run)."""
        return self.run(["-o", org, "-s", name, "exec", "--no-port-forward", "--", *argv], input=input,
                        timeout=timeout, max_output=max_output)
