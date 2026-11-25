"""Vercel Sandbox's manifest: what it has (lib/cloud/capabilities.py; providers/vercel/adapter.py says what a sandbox
is), and what the command line needs from it."""
from cloud.ubuntu_vm import BASE

# A sandbox's size: vCPUs x GB of memory (2 GB per vCPU, fixed by Vercel). Hobby allows 4x8 at most; 8x16 needs Pro.
SIZES = {"2x4": 2, "4x8": 4, "8x16": 8}
DEFAULT_SIZE = "2x4"
# Where Vercel runs sandboxes (vercel.com/docs/sandbox/concepts/regions, read 2026-11-25). A sandbox's snapshots stay
# in its region, so a computer's region is fixed when it's made. None given: the project's default (iad1 unless set).
REGIONS = ("iad1", "arn1", "bom1", "cdg1", "cle1", "cpt1", "dub1", "fra1", "gru1", "hkg1", "hnd1", "icn1", "kix1",
           "lhr1", "pdx1", "sfo1", "sin1", "syd1", "yul1")

MANIFEST = dict(
    BASE, name="vercel", place="vercel", label="Vercel Sandbox",
    root_disk="persistent",
    # Snapshots of the whole disk. Taking one stops the computer (Vercel's snapshot ends the session), and so does a
    # restore: either way open apps close, and the desktop is started again.
    snapshots={"keep": None, "what": "snapshots of the whole disk; taking one or restoring one restarts the computer "
                                     "(open apps close)"},
    # A stop snapshots the disk and powers the VM off; the next start boots from that snapshot: files are kept, no
    # program is. The session's timeout is the idle stop (extended while it's in use, up to the plan's cap: 45 minutes
    # on Hobby, 24 hours on Pro).
    sleep_wake={"kind": "stop-start", "keeps_processes": False},
    # Vercel's network policy (names matched by TLS SNI, and address ranges), enforced outside the VM.
    egress={"model": "network-policy", "allowlist": True, "private_ranges_blocked": True},
    # Enforced outside the VM (nothing in it, root included, can change it): EGRESS=internet lets every name and
    # address out but private, shared, link-local (the metadata service) and reserved ranges, which Vercel's policy
    # refuses (checked live: docs/vercel.md); EGRESS=off is Vercel's deny-all.
    outside_egress={"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    # Stopped, a sandbox bills its snapshot's storage only; running, the CPU time it uses and all of its memory.
    billing={"stopped": "storage", "guest_poweroff": "n/a",
             "note": "stopped: its snapshot's storage only; running: the CPU time it uses and all of its memory (a "
                     "session ends at its timeout, IDLE_STOP after it was last used, or at the plan's cap)"},
    # The sandbox's kernel has no uinput: keys, typing and pixel clicks go in the foreground (XTEST); clicks and
    # actions on an element go through accessibility, as anywhere.
    input={"background": False, "note": "no /dev/uinput in a Vercel sandbox: keys, typing and pixel clicks go in the "
                                        "foreground; actions on an element through accessibility"},
    sizes=tuple(SIZES), regions=REGIONS, fresh_start=False)

# What the command line needs from it (lib/cloud/manifests.py says what each is). Its credentials reach lib/remote.py
# and the transport's broker only (providers/vercel/broker.py): never ssh, the hub or any other program.
CLI = {
    "owners": ("@jonradoff",),
    "place_words": "vercel (Vercel Sandbox)",
    "docs": "docs/vercel.md",
    "credentials": ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID"),
    "credential_words": "VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID",
    "api_host": "api.vercel.com",
    "transport": "vercel",
    "transport_settings": ("VERCEL_SANDBOX",),
    # (OPTION, WHAT IT NEEDS, ITS DEFAULT FROM THE ENVIRONMENT, ONLY FOR THIS PROVIDER)
    "create_options": (("--sandbox", "the sandbox's name", "", True),),
    "teardown": False,
    "words": {
        "vm_create_confirm": "This makes the sandbox at Vercel, then installs the desktop (about 6 minutes).",
        "instance_word": "sandbox",
        "status_baseline": "a snapshot at Vercel (metacomputer checkpoints)",
        "status_network": "Vercel's network policy: EGRESS={egress} (net status reads what's in force), SSH only "
                          "through Vercel's API   stop: the session's timeout (IDLE_STOP) or metacomputer stop",
        "status_uinput": "  (none in a Vercel sandbox: foreground input)",
    },
}
