"""The facade of Fly's `sprite` command for the Sprites adapter (lib/cloud/sprites.py): the Sprites API through
`sprite api` (the CLI adds its own sign-in, which it keeps in the login keychain: metacomputer never sees the token),
and `sprite exec` for the setup's first step. Only lib/remote.py makes one; the adapter takes it as a value, so tests
hand it a stand-in. Plain values in and out; nothing the command prints is passed on whole."""
import json
import os
import re
import subprocess

from cloud.adapter import AdapterError


class SpriteCLI:
    """The calls the adapter makes, through Fly's `sprite` command: plain values in and out, so a stand-in can take
    its place. The command runs with a small environment of its own (HOME and PATH: it reads its own sign-in), never
    the caller's, and nothing it prints is passed on whole."""

    def __init__(self, path, timeout=120):
        self.path = path
        self.timeout = timeout

    def env(self):
        return {"HOME": os.environ.get("HOME") or os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8"}

    def run(self, args, input=None, timeout=None):
        try:
            r = subprocess.run([self.path, *args], input=input, capture_output=True, timeout=timeout or self.timeout,
                               env=self.env())
        except FileNotFoundError:
            raise AdapterError(f"Fly's sprite command isn't at {self.path}") from None
        except subprocess.TimeoutExpired:
            raise AdapterError(f"Fly's sprite command didn't answer in {timeout or self.timeout} s") from None
        return r

    def api(self, org, path, method="GET", body=None, timeout=None):
        """(HTTP status, body text) of a Sprites API call, through `sprite api` (the CLI adds its own sign-in)."""
        args = ["-o", org, "api", path, "--", "-sS", "-X", method, "-w", "\n%{http_code}"]
        if body is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        r = self.run(args, input=json.dumps(body).encode() if body is not None else None, timeout=timeout)
        out = r.stdout.decode(errors="replace")
        text, _, code = out.rstrip("\n").rpartition("\n")
        if r.returncode != 0 or not code.isdigit():
            why = " ".join(r.stderr.decode(errors="replace").split())[-200:]
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

    def create(self, org, name):
        d = self._json(org, "/v1/sprites", "POST", {"name": name}, ok=(200, 201))
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

    def exec(self, org, name, argv, input=None, timeout=None):
        """`sprite exec` (no port forwards back to this Mac): returns (exit status, stdout bytes, stderr bytes)."""
        r = self.run(["-o", org, "-s", name, "exec", "--no-port-forward", "--", *argv], input=input, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
