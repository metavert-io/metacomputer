"""Cloudflare's REST API (api.cloudflare.com/client/v4), as metacomputer uses it to put a computer's control Worker into
the user's own account and take it out again: the calls Cloudflare's own wrangler makes for a Worker whose Durable
Object runs a container (the durable_object scheduling policy), as plain values, so a stand-in answers for it in the
tests.

    workers.dev         the account's subdomain; a script's workers.dev address turned on (and nothing else: no route)
    scripts             one script uploaded (its two modules, its bindings, its container and its secret), read back,
                        deleted; never another script, which nothing here lists by content or changes
    Durable Objects     the namespace the upload made for the script's class (its id names the container application)
    Containers          the application of that namespace (Cloudflare's SSH turned off), an image prepared to run,
                        the registry's short-lived credentials (images.py), an instance's SSH endpoint (to check it's off)

Its credentials are given as values (lib/remote.py hands them over from its own environment, which nothing else
inherits): CLOUDFLARE_API_TOKEN (an API token: docs/cloudflare.md says which permissions) and CLOUDFLARE_ACCOUNT_ID
(32 hex digits). Neither is ever put in a message: everything Cloudflare says passes scrub() (the token, the account's ID
and anything credential-shaped replaced). An image's reference names the account, so it's built here and never said.

HTTP goes through `http` (lib/provider_http.py send, or a test's stand-in): nothing here opens a connection. A call that
makes or changes something is sent once (lib/cloud/vps.py API.call); reads and deletes are tried again on throttling and
passing errors."""
import contextlib
import json
import os
import re

from cloud.adapter import AdapterError
from cloud.vps import API as RestAPI, ProviderError

HOST = "api.cloudflare.com"
BASE = "https://" + HOST + "/client/v4"
REGISTRY = "registry.cloudflare.com"
ACCOUNT = re.compile(r"[0-9a-f]{32}")
SCRIPT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
NAMESPACE = re.compile(r"[0-9a-f]{32}")
REPO = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CLASS = "Computer"
COMPAT_DATE = "2026-11-08"
MAX_PAGES = 20


class CloudflareError(ProviderError):
    """Cloudflare refused a call: its status, its error code and its message, scrubbed."""


def script_name(n):
    if not isinstance(n, str) or not SCRIPT.fullmatch(n):
        raise AdapterError(f"{str(n)[:64]!r} isn't a Worker's name (lowercase letters, digits and dashes, 63 at most)")
    return n


def check_account(acc):
    if not ACCOUNT.fullmatch(acc or ""):
        raise AdapterError("CLOUDFLARE_ACCOUNT_ID isn't a Cloudflare account's ID (32 hex digits: the account the "
                           "computers are made in, docs/cloudflare.md)")
    return acc


def multipart(parts):
    """A multipart/form-data body: (content type, bytes) of [(field name, file name or None, its type, bytes)]."""
    boundary = "metacomputer-" + os.urandom(12).hex()
    out = bytearray()
    for name, filename, ctype, data in parts:
        out += f"--{boundary}\r\n".encode()
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"Content-Disposition: {disp}\r\n".encode()
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n" + bytes(data) + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


class API(RestAPI):
    """The REST API of one Cloudflare account."""
    label = "Cloudflare"
    base = BASE
    env_key = "CLOUDFLARE_API_TOKEN"

    def __init__(self, env=None, http=None, sleep=None, clock=None):
        env = env or {}
        super().__init__(env, http, sleep, clock)
        self.account = check_account((env.get("CLOUDFLARE_ACCOUNT_ID") or "").strip().lower())
        self.__tok = env["CLOUDFLARE_API_TOKEN"].strip()

    def scrub(self, text, cap=300):
        out = super().scrub(text, cap * 4)
        out = out.replace(self.account, "<account>")
        out = re.sub(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", "<account>", out)
        return out[:cap]

    def refused(self, status, answer, what=""):
        code, message = "", ""
        with contextlib.suppress(ValueError, AttributeError, TypeError, IndexError, KeyError):
            j = json.loads(answer)
            errs = j.get("errors") if isinstance(j, dict) else None
            if isinstance(errs, list) and errs:
                e = errs[0] if isinstance(errs[0], dict) else {"message": str(errs[0])}
                code, message = e.get("code", ""), e.get("message", "")
            elif isinstance(j, dict):
                code, message = j.get("code", "") or j.get("error", ""), j.get("message", "") or j.get("detail", "")
        return CloudflareError(self.label, status, code, message, what)

    def acc(self, path):
        return f"/accounts/{self.account}{path}"

    def result(self, method, path, body=None, query=None, missing_ok=False, what=""):
        """One call's `result` (Cloudflare's envelope); None for a 404 when missing_ok."""
        j = self.call(method, self.acc(path), body=body, query=query, missing_ok=missing_ok, what=what)
        if j is None:
            return None
        if isinstance(j, dict) and j.get("success") is False:
            raise self._scrubbed(self.refused(200, json.dumps(j), what))
        return j.get("result") if isinstance(j, dict) else None

    def send_raw(self, method, path, body, ctype, query="", what=""):
        """A call with a body that isn't JSON (a script's upload): sent once, its result."""
        url = self.base + self.acc(path) + (("?" + query) if query else "")
        headers = {"Authorization": f"Bearer {self.__tok}", "Accept": "application/json", "User-Agent": "metacomputer",
                   "Content-Type": ctype}
        try:
            status, _h, answer = self._http(method, url, headers, body, 120)
        except OSError as e:
            from cloud.vps import LaunchUnknown
            raise LaunchUnknown(f"{self.label}'s API didn't answer ({self.scrub(e)})") from None
        if not 200 <= status < 300:
            raise self._scrubbed(self.refused(status, answer, what))
        try:
            j = json.loads(answer)
        except ValueError:
            raise AdapterError(f"{self.label}'s API answered with something that isn't JSON") from None
        return j.get("result") if isinstance(j, dict) else None

    # --- workers.dev ---------------------------------------------------------------------------------------------
    def subdomain(self):
        r = self.result("GET", "/workers/subdomain", what="reading the account's workers.dev subdomain") or {}
        s = r.get("subdomain")
        if not isinstance(s, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s):
            raise AdapterError("this Cloudflare account has no workers.dev subdomain yet: open Workers & Pages in its "
                               "dashboard once (it's made there), then try again")
        return s

    def enable_workers_dev(self, name):
        self.result("POST", f"/workers/scripts/{script_name(name)}/subdomain",
                    body={"enabled": True, "previews_enabled": False}, what="turning on its workers.dev address")

    # --- scripts ---------------------------------------------------------------------------------------------------
    def script_settings(self, name):
        """The script's settings (its bindings: plain text values readable, secrets not), or None when there's none."""
        return self.result("GET", f"/workers/scripts/{script_name(name)}/settings", missing_ok=True,
                           what="reading the Worker's settings")

    @staticmethod
    def plain(settings, key):
        for b in (settings or {}).get("bindings") or []:
            if isinstance(b, dict) and b.get("name") == key and b.get("type") == "plain_text":
                return b.get("text")
        return None

    def upload(self, name, modules, plain, secret, repo_digest_ref, size_word):
        """The control Worker, made or replaced: its modules ({file name: bytes}, index.js the main one), plain-text
        bindings, its one secret (the computer's key, which reaches Cloudflare here and nowhere else), the Durable
        Object class Computer with its container (the image, by digest), on SQLite storage. Sent once."""
        script_name(name)
        meta = {
            "main_module": "index.js",
            "bindings": [{"name": k, "type": "plain_text", "text": v} for k, v in sorted(plain.items())]
                        + [{"name": "MCOMP_KEY", "type": "secret_text", "text": secret},
                           {"name": "COMPUTER", "type": "durable_object_namespace", "class_name": CLASS}],
            "containers": [{"name": name, "class_name": CLASS, "images": {"ubuntu": repo_digest_ref}}],
            "compatibility_date": COMPAT_DATE, "compatibility_flags": [],
            "code_update_strategy": {"mode": "deferred", "max_delay": 300},
            "exports": {CLASS: {"type": "durable-object", "storage": "sqlite", "container": name}},
            "observability": {"enabled": False},
        }
        parts = [("metadata", None, "application/json", json.dumps(meta).encode())]
        for fname, data in modules.items():
            parts.append((fname, fname, "application/javascript+module", data))
        ctype, body = multipart(parts)
        del meta, parts
        return self.send_raw("PUT", f"/workers/scripts/{name}", body, ctype, query="excludeScript=true",
                             what="uploading the control Worker") or {}

    def delete_script(self, name):
        """True when it was deleted, False when there was none."""
        j = self.call("DELETE", self.acc(f"/workers/scripts/{script_name(name)}"), query={"force": "true"},
                      missing_ok=True, what="deleting the control Worker")
        return j is not None

    def scripts(self):
        """The account's script names (only to see whether one of a name exists)."""
        r = self.result("GET", "/workers/scripts", what="listing the account's Workers") or []
        return [s.get("id") for s in r if isinstance(s, dict) and isinstance(s.get("id"), str)]

    # --- Durable Objects -------------------------------------------------------------------------------------------
    def namespace_of(self, name):
        """The id of the Durable Object namespace of the script's class Computer, or None."""
        script_name(name)
        for page in range(1, MAX_PAGES + 1):
            r = self.result("GET", "/workers/durable_objects/namespaces", query={"per_page": 1000, "page": page},
                            what="listing Durable Object namespaces") or []
            for ns in r:
                if isinstance(ns, dict) and ns.get("script") == name and ns.get("class") == CLASS \
                        and NAMESPACE.fullmatch(str(ns.get("id") or "")):
                    return ns["id"]
            if len(r) < 1000:
                return None
        return None

    # --- Containers ------------------------------------------------------------------------------------------------
    def prepare_image(self, ref):
        """Asks Cloudflare to make the image ready to run (it converts it once): its status, pending|ready|error."""
        r = self.result("POST", "/containers/image-preparations", body={"image": ref},
                        what="preparing the image to run") or {}
        return str(r.get("status") or "").lower(), self.scrub(r.get("reason") or "", 200)

    def application(self, app_id):
        if not NAMESPACE.fullmatch(app_id or ""):
            raise AdapterError("not a container application's id")
        return self.result("GET", f"/containers/applications/{app_id}", missing_ok=True,
                           what="reading the container application")

    def create_application(self, name, namespace_id):
        """The container application of the namespace (its id is the namespace's), Cloudflare's own SSH off."""
        return self.result("POST", "/containers/applications", body={
            "name": script_name(name), "scheduling_policy": "durable_object",
            "durable_objects": {"namespace_id": namespace_id},
            "configuration": {"wrangler_ssh": {"enabled": False}}}, what="making the container application") or {}

    def ssh_off(self, app_id):
        """Turns Cloudflare's SSH off on an application whose configuration says otherwise (sent once)."""
        return self.result("PATCH", f"/containers/applications/{app_id}",
                           body={"configuration": {"wrangler_ssh": {"enabled": False}}},
                           what="turning Cloudflare's SSH off") or {}

    def delete_application(self, app_id):
        if not NAMESPACE.fullmatch(app_id or ""):
            raise AdapterError("not a container application's id")
        j = self.call("DELETE", self.acc(f"/containers/applications/{app_id}"), missing_ok=True,
                      what="deleting the container application")
        return j is not None

    def applications(self):
        r = self.result("GET", "/containers/applications", what="listing container applications") or []
        return [a for a in r if isinstance(a, dict)]

    def registry_credentials(self, permissions, minutes=15):
        r = self.result("POST", f"/containers/registries/{REGISTRY}/credentials",
                        body={"expiration_minutes": int(minutes), "permissions": list(permissions)},
                        what="getting the registry's short-lived credentials") or {}
        pw = r.get("password")
        if not isinstance(pw, str) or not pw:
            raise AdapterError("Cloudflare gave no registry credentials")
        user = r.get("username") if isinstance(r.get("username"), str) and r.get("username") else "v1"
        return user, pw

    def instance_ssh(self, do_id):
        """Cloudflare's SSH endpoint for an instance (by its Durable Object's id): (status, what it said, scrubbed). Read
        only to check it's off; whatever it answers is never used to connect."""
        if not re.fullmatch(r"[0-9a-f]{64}", do_id or ""):
            raise AdapterError("not a Durable Object's id")
        try:
            r = self.result("GET", f"/containers/instances/{do_id}/ssh", what="asking for Cloudflare's SSH")
            return 200, "an SSH endpoint was given" if r else "an empty answer"
        except CloudflareError as e:
            return e.status, self.scrub(f"{e.code} {e.message}", 160)

    def image_ref(self, repo, digest):
        """The image's reference in the account's registry (it names the account: never said)."""
        if not REPO.fullmatch(repo or "") or not DIGEST.fullmatch(digest or ""):
            raise AdapterError("not an image in the account's registry")
        return f"{REGISTRY}/{self.account}/{repo}@{digest}"
