"""The computer's image in the account's own container registry, without Docker: Ubuntu 24.04 (amd64) copied from Docker
Hub, pinned by its manifest's digest, into registry.cloudflare.com/<account>/<repository>, over the registries' HTTP API
(OCI distribution). Also what's in a repository (the snapshots Cloudflare keeps there), and the image's manifest
deleted with the computer.

- Docker Hub: an anonymous pull token for library/ubuntu; the manifest read by the pinned digest and its bytes hashed
  (they must be that digest); each blob read by its digest (Docker Hub answers with a redirect to its download
  address, followed once, to https only and with no credential) and hashed before it's sent on.
- Cloudflare's registry: short-lived credentials from Cloudflare's API (api.py registry_credentials), held in memory;
  each blob uploaded only if the repository hasn't it; the manifest put by its digest. Uploads go only to
  registry.cloudflare.com (an upload address elsewhere is refused before the credentials go to it).
Snapshot manifests can't be deleted (Cloudflare refuses: 403), nor the image's conversion (409): delete_image deletes
only the manifest metacomputer put there, and says what's left."""
import base64
import hashlib
import json
import re

from . import wire

HUB = "registry-1.docker.io"
HUB_AUTH = "auth.docker.io"
CF = "registry.cloudflare.com"
SOURCE_REPO = "library/ubuntu"
MANIFEST_TYPES = ", ".join(["application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json",
                            "application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json"])
MAX_BLOB = 256 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


class Registries:
    """Docker Hub (anonymous) and the account's registry. `send` is wire.https's signature (a stand-in in tests)."""

    def __init__(self, api, send=None):
        self.api = api
        self.account = api.account
        self._send = send or wire.https
        self.__auth = None

    def scrub(self, text, cap=300):
        return self.api.scrub(str(text or "").replace(self.__auth or "\0", "[redacted]"), cap)

    # --- Docker Hub ---------------------------------------------------------------------------------------------
    def hub_token(self):
        st, _h, body = self._send(HUB_AUTH, "GET", f"/token?service=registry.docker.io&scope=repository:{SOURCE_REPO}:pull",
                                  {"User-Agent": "metacomputer"}, None, 60, 1 << 20)
        try:
            tok = json.loads(body).get("token") if st == 200 else None
        except ValueError:
            tok = None
        if not isinstance(tok, str) or not tok:
            raise wire.WireError(st, "Docker Hub didn't give a pull token for Ubuntu's image")
        return tok

    def hub_get(self, path, token, accept=None, cap=MAX_BLOB):
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "metacomputer"}
        if accept:
            headers["Accept"] = accept
        st, h, body = self._send(HUB, "GET", path, headers, None, 300, cap)
        if st in (301, 302, 303, 307, 308):
            loc = h.get("location") or ""
            m = re.fullmatch(r"https://([A-Za-z0-9.-]+)(/[^\s]*)", loc)
            if not m:
                raise wire.WireError(st, "Docker Hub redirected somewhere that isn't an https address")
            st, h, body = self._send(m.group(1), "GET", m.group(2), {"User-Agent": "metacomputer"}, None, 300, cap)
        if st != 200:
            raise wire.WireError(st, f"Docker Hub answered {st} for {path.split('/')[-1][:24]}")
        return h, body

    # --- the account's registry ---------------------------------------------------------------------------------
    def login(self, permissions=("push", "pull")):
        user, pw = self.api.registry_credentials(list(permissions))
        self.__auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        del pw

    def cf(self, method, path, headers=None, body=None, cap=1 << 20, timeout=300):
        if self.__auth is None:
            self.login()
        h = {"Authorization": self.__auth, "User-Agent": "metacomputer", **(headers or {})}
        try:
            return self._send(CF, method, path, h, body, timeout, cap)
        except wire.WireError as e:
            raise wire.WireError(e.status, self.scrub(e)) from None

    def base(self, repo):
        return f"/v2/{self.account}/{repo}"

    def has_blob(self, repo, digest):
        st, _h, _b = self.cf("HEAD", f"{self.base(repo)}/blobs/{digest}")
        return st == 200

    def push_blob(self, repo, digest, data):
        st, h, body = self.cf("POST", f"{self.base(repo)}/blobs/uploads/", body=b"")
        if st != 202:
            raise wire.WireError(st, f"the registry didn't start an upload ({self.scrub(body[:200])})")
        loc = h.get("location") or ""
        if loc.startswith("https://"):
            host, _, rest = loc[len("https://"):].partition("/")
            if host != CF:
                raise wire.WireError(st, "the registry's upload address isn't on registry.cloudflare.com: refused")
            loc = "/" + rest
        if not loc.startswith("/v2/"):
            raise wire.WireError(st, "the registry gave no upload address")
        sep = "&" if "?" in loc else "?"
        st, _h, body = self.cf("PUT", f"{loc}{sep}digest={digest}", body=data,
                               headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})
        if st != 201:
            raise wire.WireError(st, f"the registry didn't take a layer ({self.scrub(body[:200])})")

    def put_manifest(self, repo, ref, data, ctype):
        st, h, body = self.cf("PUT", f"{self.base(repo)}/manifests/{ref}", body=data, headers={"Content-Type": ctype})
        if st != 201:
            raise wire.WireError(st, f"the registry didn't take the image's manifest ({self.scrub(body[:200])})")
        return h.get("docker-content-digest") or sha(data)

    def has_manifest(self, repo, ref):
        st, _h, _b = self.cf("HEAD", f"{self.base(repo)}/manifests/{ref}", headers={"Accept": MANIFEST_TYPES})
        return st == 200

    def tags(self, repo):
        st, _h, body = self.cf("GET", f"{self.base(repo)}/tags/list", headers={"Accept": "application/json"})
        if st == 404:
            return []
        if st != 200:
            raise wire.WireError(st, "the registry didn't list the repository")
        try:
            return [t for t in json.loads(body).get("tags") or [] if isinstance(t, str)]
        except (ValueError, AttributeError):
            return []

    def manifest(self, repo, ref):
        st, h, body = self.cf("GET", f"{self.base(repo)}/manifests/{ref}", headers={"Accept": MANIFEST_TYPES})
        if st != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None

    def delete_manifest(self, repo, digest):
        """(status, Cloudflare's code): 202 deleted, 404 gone already; 403/409 for what Cloudflare keeps."""
        st, _h, body = self.cf("DELETE", f"{self.base(repo)}/manifests/{digest}", headers={"Accept": MANIFEST_TYPES})
        code = ""
        try:
            j = json.loads(body)
            code = (j.get("errors") or [{}])[0].get("code") or j.get("code") or ""
        except (ValueError, AttributeError, IndexError, TypeError):
            pass
        return st, str(code)[:60]

    def gc(self):
        st, _h, _b = self.cf("PUT", "/v2/gc/layers", headers={"Content-Type": "application/json"}, body=b"")
        return st

    # --- what metacomputer does with them -----------------------------------------------------------------------
    def copy_image(self, repo, digest, log=None):
        """Ubuntu's image (the manifest of `digest`, linux/amd64) into the account's repository `repo`, each part
        checked by its digest. Returns the digest the registry has it under (the same)."""
        if not DIGEST.fullmatch(digest or ""):
            raise wire.WireError(0, "the image's pin isn't a sha256 digest")
        say = log or (lambda m: None)
        self.login()
        if self.has_manifest(repo, digest):
            say("the image is in the account's registry already")
            return digest
        tok = self.hub_token()
        h, man = self.hub_get(f"/v2/{SOURCE_REPO}/manifests/{digest}", tok, MANIFEST_TYPES, cap=1 << 20)
        if sha(man) != digest:
            raise wire.WireError(0, "Docker Hub's manifest for Ubuntu isn't the pinned one: nothing was copied")
        m = json.loads(man)
        if m.get("mediaType") not in ("application/vnd.oci.image.manifest.v1+json",
                                      "application/vnd.docker.distribution.manifest.v2+json"):
            raise wire.WireError(0, "the pinned digest isn't one platform's image manifest")
        ctype = h.get("content-type") or m["mediaType"]
        for blob in [m["config"], *m["layers"]]:
            d = blob.get("digest")
            if not DIGEST.fullmatch(d or "") or int(blob.get("size") or 0) > MAX_BLOB:
                raise wire.WireError(0, "the image's manifest names a part that isn't one")
            if self.has_blob(repo, d):
                continue
            _h, data = self.hub_get(f"/v2/{SOURCE_REPO}/blobs/{d}", tok)
            if sha(data) != d:
                raise wire.WireError(0, f"a part of Ubuntu's image isn't what its manifest says ({d[:19]}): nothing "
                                        "more was copied")
            self.push_blob(repo, d, data)
            say(f"copied {d[:19]} ({len(data) // 1024} KB)")
        got = self.put_manifest(repo, digest, man, ctype)
        if got != digest:
            raise wire.WireError(0, "the registry keeps the image under another digest")
        return digest

    def residue(self, repo):
        """What's in the repository: [(tag, kind, size)] (kind: image, snapshot, conversion, other)."""
        out = []
        for t in self.tags(repo):
            m = self.manifest(repo, t) or {}
            ctype = str((m.get("config") or {}).get("mediaType") or "")
            size = sum(int(x.get("size") or 0) for x in m.get("layers") or [] if isinstance(x, dict))
            kind = ("snapshot" if "snapshot" in ctype else "conversion" if "btrfs-chain" in ctype
                    else "image" if "image.config" in ctype or "container.image" in ctype else "other")
            out.append((t, kind, size))
        return out

    def delete_image(self, repo, digest):
        """The image's manifest metacomputer put in the repository, deleted, and the registry's unused layers
        collected: (deleted, what Cloudflare said)."""
        st, code = self.delete_manifest(repo, digest)
        if st in (200, 202, 204, 404):
            try:
                self.gc()
            except wire.WireError:
                pass
        return st in (200, 202, 204, 404), f"{st} {code}".strip()
