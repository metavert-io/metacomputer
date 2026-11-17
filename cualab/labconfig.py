"""Read simple KEY=VALUE settings from config/defaults.env, then this
machine's config/local.env (or $VC_CONFIG), the same files scripts/lib.sh
sources. Only plain values are understood (quotes stripped, no expansion),
which is all the Python side needs (e.g. GUEST_USER)."""
import os
import re

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s#]*)")


def load():
    vals = {}
    for p in (os.path.join(LAB, "config", "defaults.env"),
              os.environ.get("VC_CONFIG") or os.path.join(LAB, "config", "local.env")):
        try:
            for line in open(p):
                m = _LINE.match(line)
                if m:
                    vals[m.group(1)] = m.group(2).strip("\"'")
        except OSError:
            pass
    return vals


CONFIG = load()
GUEST_USER = CONFIG.get("GUEST_USER", "cua")
GUEST_HOME = os.path.join("/home", GUEST_USER)
GUEST_RUNS = os.path.join(GUEST_HOME, "cua-lab-runs")
