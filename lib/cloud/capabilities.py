"""What a provider has: the capability manifest every adapter carries, how a computer's files persist (PERSIST),
and which metacomputer features need which capabilities.

A manifest is plain data (a dict), checked by check_manifest():
    name, place, label     the adapter's name, the place its computers are (lib/placement.py), how it's shown
    beta                   whether the provider's side of it is a beta
    root_disk              "persistent" (the system disk keeps everything) or "ephemeral" (each start is fresh)
    volumes                None, or attached volumes: {"attach": "at create" | "at start" | "any time",
                           "outlive_computer": bool, "size_limit_gb": number or None, "home": bool (a home folder
                           can live on one), "mac_folders": bool (a folder of this Mac, shared)}
    snapshots              None, or native snapshots: {"keep": a number or None (no limit), "what": text}
    backup                 None, or saved copies in object storage: {"to": text, "retention_days": number or None}
    bucket_mount           None, or a storage bucket as a folder: {"how": text, "home": bool (whether it can hold a
                           home folder or browser profile: needs locks and atomic renames)}
    sleep_wake             {"kind": "stop-start" | "auto-sleep" | "none", "keeps_processes": bool}
    egress                 {"model": one of EGRESS_MODELS, "allowlist": bool, "private_ranges_blocked": bool}:
                           mac-gateway (this Mac's gateway), guest-firewall (rules in the computer itself, set up
                           by metacomputer: lib/cloud/guest.py), or what the provider enforces from outside
    outside_egress         what is enforced OUTSIDE the computer, which root inside it can't undo: {"enforced": bool
                           (anything at all), "private_ranges": bool (private, shared and reserved ranges refused),
                           "metadata": bool (the cloud's metadata service refused), "off": bool (all egress can be
                           turned off from outside)}. Never more than the provider really enforces: what's only in
                           the guest's own firewall doesn't count here
    billing                what a computer costs when it isn't running: {"stopped": "none" | "storage" | "full" |
                           "unknown" (what a computer stopped through the provider still bills), "guest_poweroff":
                           "stops-billing" | "nothing" | "n/a" (what the guest powering itself off saves, which is
                           what an idle stop does), "note": text}
    input                  how Cua Driver's input reaches the desktop: {"background": bool (input that doesn't take
                           the focus: the driver's virtual devices, which need /dev/uinput), "note": text}. Without
                           it, keys, typing and pixel clicks go in the foreground (the desktop's own pointer and
                           keyboard); clicks and actions on an element still go through accessibility
    port_forward           bool: one of the computer's ports can be reached on this Mac's loopback
    sizes, regions         tuples of what create() takes (empty: not chosen per computer)
    fresh_start            bool: the computer can start from its image every time (PERSIST=none)

PERSIST=auto|disk|volume|backup|none is a computer's setting: disk (the root disk keeps everything), volume (the home
folder is on an attached volume; the system starts fresh from the image), backup (its files are saved to object
storage when it sleeps or stops and put back when it starts), none (fresh every start). auto picks the first of
those the manifest offers; a mode it doesn't offer is refused, saying which it does."""

ROOT_DISK = ("persistent", "ephemeral")
SLEEP_KINDS = ("stop-start", "auto-sleep", "none")
ATTACH = ("at create", "at start", "any time")
EGRESS_MODELS = ("mac-gateway", "guest-firewall", "dns-allowlist", "network-policy", "http-intercept", "open")
PERSIST_MODES = ("disk", "volume", "backup", "none")      # auto's order, best first
PERSIST_WORDS = {
    "disk": "the root disk keeps everything",
    "volume": "the home folder is on an attached volume; the system starts fresh from the image",
    "backup": "its files are saved to object storage when it sleeps or stops, and put back when it starts",
    "none": "it starts fresh every time: nothing is kept",
}
KEYS = ("name", "place", "label", "beta", "root_disk", "volumes", "snapshots", "backup", "bucket_mount", "sleep_wake",
        "egress", "outside_egress", "billing", "input", "port_forward", "sizes", "regions", "fresh_start")
BILLING_STOPPED = ("none", "storage", "full", "unknown")
BILLING_POWEROFF = ("stops-billing", "nothing", "n/a")


class Unavailable(ValueError):
    """A persistence mode or feature this provider doesn't offer; str() says why and what it does offer."""


def check_manifest(m):
    """The problems with a manifest, as a list of strings (empty: it's sound)."""
    if not isinstance(m, dict):
        return ["a manifest is a dict"]
    bad = [f"missing {k}" for k in KEYS if k not in m]
    bad += [f"unknown key {k}" for k in m if k not in KEYS]
    if bad:
        return bad

    def kind(k, ok, what):
        if not ok:
            bad.append(f"{k}: {what}, not {m[k]!r}")

    def opt_dict(k, need):
        v = m[k]
        if v is None:
            return
        if not isinstance(v, dict):
            bad.append(f"{k}: None or a dict, not {v!r}")
            return
        for key, test, what in need:
            if key not in v or not test(v[key]):
                bad.append(f"{k}.{key}: {what}")

    num_or_none = (lambda x: x is None or (isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0))
    is_bool = (lambda x: isinstance(x, bool))
    is_text = (lambda x: isinstance(x, str) and bool(x.strip()))
    for k in ("name", "place", "label"):
        kind(k, is_text(m[k]), "text")
    for k in ("beta", "port_forward", "fresh_start"):
        kind(k, is_bool(m[k]), "True or False")
    kind("root_disk", m["root_disk"] in ROOT_DISK, " or ".join(ROOT_DISK))
    opt_dict("volumes", [("attach", lambda x: x in ATTACH, " or ".join(ATTACH)),
                         ("outlive_computer", is_bool, "True or False"),
                         ("size_limit_gb", num_or_none, "a number of GB or None"),
                         ("home", is_bool, "True or False"), ("mac_folders", is_bool, "True or False")])
    opt_dict("snapshots", [("keep", lambda x: x is None or (isinstance(x, int) and not isinstance(x, bool) and x > 0),
                            "a count or None"), ("what", is_text, "text")])
    opt_dict("backup", [("to", is_text, "text"), ("retention_days", num_or_none, "days or None")])
    opt_dict("bucket_mount", [("how", is_text, "text"), ("home", is_bool, "True or False")])
    sw = m["sleep_wake"]
    if not isinstance(sw, dict) or sw.get("kind") not in SLEEP_KINDS or not is_bool(sw.get("keeps_processes")):
        bad.append(f"sleep_wake: {{'kind': {' | '.join(SLEEP_KINDS)}, 'keeps_processes': bool}}")
    eg = m["egress"]
    if (not isinstance(eg, dict) or eg.get("model") not in EGRESS_MODELS or not is_bool(eg.get("allowlist"))
            or not is_bool(eg.get("private_ranges_blocked"))):
        bad.append(f"egress: {{'model': {' | '.join(EGRESS_MODELS)}, 'allowlist': bool, 'private_ranges_blocked': bool}}")
    oe = m["outside_egress"]
    if not isinstance(oe, dict) or set(oe) != {"enforced", "private_ranges", "metadata", "off"} \
            or not all(is_bool(v) for v in oe.values()):
        bad.append("outside_egress: {'enforced': bool, 'private_ranges': bool, 'metadata': bool, 'off': bool}")
    elif not oe["enforced"] and any(oe[k] for k in ("private_ranges", "metadata", "off")):
        bad.append("outside_egress: nothing is enforced outside, so nothing can be claimed for it")
    bl = m["billing"]
    if (not isinstance(bl, dict) or bl.get("stopped") not in BILLING_STOPPED
            or bl.get("guest_poweroff") not in BILLING_POWEROFF or not is_text(bl.get("note"))):
        bad.append(f"billing: {{'stopped': {' | '.join(BILLING_STOPPED)}, 'guest_poweroff': "
                   f"{' | '.join(BILLING_POWEROFF)}, 'note': text}}")
    ip = m["input"]
    if not isinstance(ip, dict) or set(ip) != {"background", "note"} or not is_bool(ip.get("background")) \
            or not is_text(ip.get("note")):
        bad.append("input: {'background': bool, 'note': text}")
    for k in ("sizes", "regions"):
        kind(k, isinstance(m[k], tuple) and all(is_text(x) for x in m[k]), "a tuple of names")
    return bad


def offers(m, mode):
    """Whether the manifest offers persistence `mode`."""
    if mode == "disk":
        return m["root_disk"] == "persistent"
    if mode == "volume":
        return bool(m["volumes"]) and m["volumes"]["home"]
    if mode == "backup":
        return bool(m["backup"])
    if mode == "none":
        return bool(m["fresh_start"])
    return False


def persist_modes(m):
    """The persistence modes the manifest offers, best first."""
    return tuple(p for p in PERSIST_MODES if offers(m, p))


def choose_persist(m, wanted="auto"):
    """PERSIST for a new computer: `auto` is the best the manifest offers; another mode must be one it offers.
    Unavailable otherwise, saying which it does offer."""
    have = persist_modes(m)
    if wanted in (None, "", "auto"):
        if not have:
            raise Unavailable(f"{m['label']} offers no way to keep a computer's files, nor to start one fresh")
        return have[0]
    if wanted not in PERSIST_MODES:
        raise Unavailable(f"PERSIST={wanted} isn't a mode: auto, {', '.join(PERSIST_MODES)}")
    if wanted not in have:
        raise Unavailable(f"{m['label']} doesn't offer PERSIST={wanted}; it offers: {', '.join(have) or 'none'}")
    return wanted


# Each feature, what it needs and how it's said when it's missing. A need is a capability (has()) or "keeps_files"
# (the computer's PERSIST keeps its files).
FEATURES = {
    "reset": (("snapshots",), "going back to the clean baseline (metacomputer reset) needs snapshots"),
    "checkpoint": (("snapshots",), "saving a checkpoint needs snapshots"),
    "restore": (("snapshots",), "restoring a checkpoint needs snapshots"),
    "keep_files": (("keeps_files",), "keeping your files between sessions needs PERSIST=disk, volume or backup"),
    "volumes": (("volumes",), "attaching a volume needs volumes"),
    "mac_folders": (("mac_folders",), "sharing a folder of this Mac with the computer needs Mac folders"),
    "s3_folders": (("bucket_mount",), "a folder kept in step with a storage bucket needs bucket folders"),
    "view": (("port_forward",), "the live view needs a port forward to this Mac"),
    "net_allowlist": (("egress_allowlist",), "a network allowlist (metacomputer net allow) needs an egress allowlist"),
    "sleep": (("sleep",), "putting the computer to sleep needs sleep and wake"),
    "background_input": (("background_input",), "input that leaves the focus alone (background keys, typing and pixel "
                                                 "clicks) needs the driver's virtual devices (/dev/uinput)"),
    "recordings": ((), "recordings stream to this Mac, so they need nothing of the provider"),
}


def has(m, cap, persist=None):
    """Whether the manifest (and, for keeps_files, the computer's PERSIST) has capability `cap`."""
    if cap == "keeps_files":
        mode = persist or (persist_modes(m) or ("none",))[0]
        return mode in ("disk", "volume", "backup") and offers(m, mode)
    if cap == "mac_folders":
        return bool(m["volumes"]) and m["volumes"]["mac_folders"]
    if cap == "egress_allowlist":
        return m["egress"]["allowlist"]
    if cap == "sleep":
        return m["sleep_wake"]["kind"] != "none"
    if cap == "port_forward":
        return m["port_forward"]
    if cap == "background_input":
        return m["input"]["background"]
    if cap in ("volumes", "snapshots", "backup", "bucket_mount"):
        return bool(m[cap])
    raise KeyError(cap)


def missing(m, feature, persist=None):
    """None when the manifest has what `feature` needs; else the plain-language reason it can't be done there."""
    needs, why = FEATURES[feature]
    lacking = [c for c in needs if not has(m, c, persist)]
    if not lacking:
        return None
    if lacking == ["keeps_files"] and persist and any(p != "none" for p in persist_modes(m)):
        return f"{why}; this computer has PERSIST={persist}"
    return f"{why}, which {m['label']} doesn't have"
