"""Cloudflare Containers' manifest: what it has (lib/cloud/capabilities.py; providers/cloudflare/adapter.py says what a
computer there is), and what the command line needs from it."""
from cloud.ubuntu_vm import BASE

# A container's size (Cloudflare's instance types within its limits: 4 vCPU, 12 GiB, 20 GB): (vCPUs, GiB, GB of disk).
SIZES = {"standard-2": (1, 6, 12), "standard-3": (2, 8, 16), "standard-4": (4, 12, 20)}
DEFAULT_SIZE = "standard-3"
# Ubuntu 24.04's image for linux/amd64 on Docker Hub, by its manifest's digest (read 2026-11-26: 24.04.5). Copied into
# the account's own registry (providers/cloudflare/images.py); every part is checked against it.
IMAGE_SOURCE = "docker.io/library/ubuntu:24.04"
IMAGE_DIGEST = "sha256:9a04f13b3f80ef056361e30868593b1cfd2279fe19e4081cb93a90bf56819d6a"

MANIFEST = dict(
    BASE, name="cloudflare", place="cloudflare", label="Cloudflare Containers",
    # Containers on the durable_object scheduling policy and container snapshots are Cloudflare's public betas.
    beta=True,
    # Cloudflare gives every start a fresh disk; metacomputer's stop snapshots it first and its start restores that
    # snapshot, so the files are kept (a stop by Cloudflare itself, which can come without warning, loses what changed
    # since the last snapshot: docs/cloudflare.md).
    root_disk="persistent",
    # Live snapshots of the whole disk: taking one doesn't interrupt the computer; restoring one restarts it. Each
    # expires 30 days after it was made or last restored (the control Worker restores the ones a computer keeps before
    # then), is tied to its image, and can't be deleted.
    snapshots={"keep": None, "what": "live snapshots of the whole disk (taking one doesn't stop it; restoring one "
                                     "restarts it); each expires 30 days after its last use and can't be deleted"},
    sleep_wake={"kind": "stop-start", "keeps_processes": False},
    # The platform's outbound rules, outside the container: enableInternet (on: the internet, private ranges and the
    # metadata address refused, checked live; off: nothing), and outbound handlers of metacomputer's own for an
    # allowlist (TLS interception: docs/cloudflare.md).
    egress={"model": "http-intercept", "allowlist": True, "private_ranges_blocked": True},
    outside_egress={"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    # Stopped: nothing for the container; its snapshots are storage, free during Cloudflare's beta.
    billing={"stopped": "storage", "guest_poweroff": "n/a",
             "note": "stopped: its snapshots' storage, free during Cloudflare's beta; running: its memory and disk "
                     "as provisioned and the CPU it uses (Workers Paid, $5 a month, includes some)"},
    input={"background": False, "note": "no /dev/uinput in a Cloudflare container: keys, typing and pixel clicks go "
                                        "in the foreground; actions on an element through accessibility"},
    port_forward=True,
    sizes=tuple(SIZES), regions=(), fresh_start=False)

# What the command line needs from it (lib/cloud/manifests.py says what each is). The account's API token is for vm
# create and vm delete only; using, starting and stopping a computer needs its control Worker's own key, which only
# lib/remote.py and the computer's broker hold (providers/cloudflare/broker.py).
CLI = {
    "owners": ("@jonradoff",),
    "place_words": "cloudflare (Cloudflare Containers)",
    "docs": "docs/cloudflare.md",
    "credentials": ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    "not_secret": (),
    "credential_words": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID",
    "api_host": "api.cloudflare.com",
    "transport": "cloudflare",
    "transport_settings": ("CLOUDFLARE_WORKER",),
    # (OPTION, WHAT IT NEEDS, ITS DEFAULT FROM THE ENVIRONMENT, ONLY FOR THIS PROVIDER)
    "create_options": (("--worker", "the control Worker's name", "", True),),
    "teardown": False,
    "words": {
        "vm_create_confirm": "This puts a control Worker for it into your Cloudflare account, then installs the "
                             "desktop (about 5 minutes).",
        "instance_word": "control Worker and container",
        "status_baseline": "a snapshot at Cloudflare (metacomputer checkpoints)",
        "status_network": "Cloudflare's outbound rules: EGRESS={egress} (net status reads what's in force), SSH only "
                          "through its control Worker   stop: IDLE_STOP after the last connection, or metacomputer stop",
        "status_uinput": "  (none in a Cloudflare container: foreground input)",
    },
}
