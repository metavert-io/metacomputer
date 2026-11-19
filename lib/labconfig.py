"""Read simple KEY=VALUE settings from config/defaults.env, then the one settings file scripts/lib.sh reads:
$MCOMP_CONFIG, else config/vms/NAME.env for MCOMP_VM=NAME, else this machine's config/local.env. Only plain values
are understood (quotes stripped, no expansion), which is all the Python side needs (e.g. GUEST_USER). scripts/lib.sh
refuses the unclear cases before anything runs, and bin/metacomputer passes a VM chosen by name (--vm) on as
MCOMP_CONFIG, so this only has to pick the same file."""
import os
import re

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMS_DIR = os.path.join(LAB, "config", "vms")
_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s#]*)")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
# What makes a VM itself: a file in config/vms/ never takes these from the defaults (scripts/lib.sh).
IDENTITY = ("VM_NAME", "BASELINE_NAME", "SSH_DIR", "SSH_PORT", "VNC_PORT", "RUN_DIR", "LOG_DIR")


def settings_path(env=None):
    env = os.environ if env is None else env
    if env.get("MCOMP_CONFIG"):
        return env["MCOMP_CONFIG"]
    name = env.get("MCOMP_VM") or ""
    if _NAME.fullmatch(name):
        p = os.path.join(VMS_DIR, name + ".env")
        if os.path.isfile(p):
            return p
    return os.path.join(LAB, "config", "local.env")


def vm_file_name(path):
    """NAME when path is config/vms/NAME.env (links in its folder resolved), else None."""
    try:
        d = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if d == os.path.realpath(VMS_DIR) and path.endswith(".env"):
            return os.path.basename(path)[:-4]
    except (OSError, ValueError):
        pass
    return None


def _parse(path):
    vals = {}
    try:
        for line in open(path):
            m = _LINE.match(line)
            if m:
                vals[m.group(1)] = m.group(2).strip("\"'")
    except OSError:
        pass
    return vals


def load(env=None):
    vals = _parse(os.path.join(LAB, "config", "defaults.env"))
    path = settings_path(env)
    vm = vm_file_name(path)
    if vm:
        for k in IDENTITY:
            vals.pop(k, None)
    vals.update(_parse(path))
    if vm:
        vals["VM_NAME"] = vals.get("VM_NAME") or vm
        vals["BASELINE_NAME"] = vals.get("BASELINE_NAME") or vals["VM_NAME"] + "-baseline"
    return vals


CONFIG = load()
GUEST_USER = CONFIG.get("GUEST_USER", "cua")
GUEST_HOME = os.path.join("/home", GUEST_USER)
GUEST_RUNS = os.path.join(GUEST_HOME, "metacomputer-runs")
