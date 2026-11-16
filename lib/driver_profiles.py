#!/usr/bin/env python3
"""Cua Driver capability manifests for DRIVER_MODE=bounded (off by default).

The templates live in config/driver-profiles/; this fills them in from the
settings bin/virtual-computer passes in the environment (VC_*), or reads your
own manifest (DRIVER_MANIFEST). In bounded mode Cua Driver refuses every tool
and resource the manifest doesn't list, and launches only apps named by their
exact absolute path; `launch_path_for` maps the names people use to those.

  driver_profiles.py render            print the manifest for the current settings
  driver_profiles.py launch-path NAME  the absolute path bounded mode needs for an app
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labconfig  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(LAB, "config", "driver-profiles")

# (names people use, launch path, process executables whose windows it opens, needs)
APPS = [
    (("firefox", "firefox-esr", "mozilla firefox"), "/usr/lib/firefox/firefox", ["/usr/lib/firefox/firefox-bin"], None),
    (("google-chrome", "google-chrome-stable", "chrome", "google chrome", "chromium"),
     "/opt/google/chrome/google-chrome", ["/opt/google/chrome/chrome"], "chrome"),
    (("mousepad", "text editor"), "/usr/bin/mousepad", ["/usr/bin/mousepad"], None),
    (("gnome-calculator", "calculator", "org.gnome.calculator"), "/usr/bin/gnome-calculator",
     ["/usr/bin/gnome-calculator"], None),
    (("libreoffice", "soffice", "libreoffice writer", "writer"), "/usr/lib/libreoffice/program/soffice",
     ["/usr/lib/libreoffice/program/soffice.bin"], None),
    (("thunar", "file manager"), "/usr/bin/thunar", ["/usr/bin/thunar"], None),
]
ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


class ProfileError(ValueError):
    pass


def env(name, default=""):
    """A setting: from bin/virtual-computer's environment (VC_NAME, resolved), else the config files."""
    v = os.environ.get("VC_" + name)
    return v if v is not None else labconfig.CONFIG.get(name, default)


def bounded(cfg=None):
    """DRIVER_MODE=bounded? From a config dict (labconfig.load()) when given, else the environment/config."""
    if cfg is not None:
        return cfg.get("DRIVER_MODE") == "bounded"
    return env("DRIVER_MODE", "standard") == "bounded"


def installed(need):
    return need is None or need in env("BROWSERS", "firefox chrome").split()


def launch_path_for(name):
    n = (name or "").strip().lower()
    for names, path, _, need in APPS:
        if n in names or n == path or n == os.path.basename(path):
            return path if installed(need) else None
    return None


def rewrite_launch(args):
    """In bounded mode, turn launch_app {"name": "firefox"} into the absolute path the manifest allows.
    Returns a note when it changed something, else None."""
    if not isinstance(args, dict) or not isinstance(args.get("name"), str):
        return None
    if any(args.get(k) for k in ("launch_path", "path", "bundle_id")):
        return None
    path = launch_path_for(args["name"])
    if not path:
        return None
    name = args.pop("name")
    args["launch_path"] = path
    return f"launched {name!r} by its path {path} (bounded mode launches apps only by absolute path)"


def _apps_block(launch):
    out = []
    for _, path, exes, need in APPS:
        if not installed(need):
            continue
        if launch:
            if path in exes:
                out.append(f"    - {{executable: {path}, launch: true, terminate: driver_launched}}")
            else:
                out.append(f"    - {{executable: {path}, launch: true}}")
                out += [f"    - {{executable: {e}, terminate: driver_launched}}" for e in exes]
        else:
            out += [f"    - {{executable: {e}}}" for e in ([path] if path in exes else exes)]
    return "\n".join(out)


def render():
    manifest = env("DRIVER_MANIFEST")
    if manifest:
        try:
            text = open(os.path.expanduser(manifest)).read()
        except OSError as e:
            raise ProfileError(f"DRIVER_MANIFEST: {e}")
        if "@" not in text:
            return text
    else:
        profile = env("DRIVER_PROFILE", "desktop") or "desktop"
        if not re.fullmatch(r"[a-z][a-z0-9-]*", profile):
            raise ProfileError(f"DRIVER_PROFILE {profile!r}: use readonly, desktop or browser")
        path = os.path.join(PROFILES, profile + ".yaml")
        if not os.path.exists(path):
            raise ProfileError(f"DRIVER_PROFILE {profile!r}: no {os.path.relpath(path, LAB)} "
                               "(readonly, desktop or browser; or set DRIVER_MANIFEST)")
        text = open(path).read()
    origins = env("DRIVER_ORIGINS").split()
    if "@ORIGINS@" in text:
        if not installed("chrome") and not manifest:
            raise ProfileError("DRIVER_PROFILE=browser drives Chrome's page tools; the VM needs Chrome "
                               "(BROWSERS=\"firefox chrome\", then rebuild)")
        if not origins:
            raise ProfileError("DRIVER_PROFILE=browser needs DRIVER_ORIGINS: the sites it may open, "
                               "e.g. DRIVER_ORIGINS=\"https://example.com https://app.example.com\"")
        bad = [o for o in origins if not ORIGIN_RE.match(o)]
        if bad:
            raise ProfileError(f"DRIVER_ORIGINS: {' '.join(bad)} isn't an origin like https://example.com")
    home = "/home/" + (env("GUEST_USER", "cua") or "cua")
    for key, val in (("@EXPIRES@", env("DRIVER_EXPIRES", "8h") or "8h"), ("@IDLE@", env("DRIVER_IDLE", "30m") or "30m"),
                     ("@HOME@", home), ("@APPS_LAUNCH@", _apps_block(True)), ("@APPS_WINDOWS@", _apps_block(False)),
                     ("@ORIGINS@", "\n".join(f"      - {o}" for o in origins))):
        text = text.replace(key, val)
    return text


def main(argv):
    if argv[:1] == ["render"]:
        try:
            sys.stdout.write(render())
        except ProfileError as e:
            print(f"virtual-computer: {e}", file=sys.stderr)
            return 1
        return 0
    if argv[:1] == ["launch-path"] and len(argv) == 2:
        p = launch_path_for(argv[1])
        if p:
            print(p)
        return 0 if p else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
