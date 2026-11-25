"""Oracle Cloud's manifest: what it has (lib/cloud/capabilities.py), its sizes, and the environment its credentials
come in (providers/oci/adapter.py says what each is)."""
from cloud.ubuntu_vm import BASE

ENV_KEYS = ("OCI_USER", "OCI_TENANCY", "OCI_FINGERPRINT", "OCI_REGION", "OCI_API_KEY_PEM", "OCI_COMPARTMENT")

# size -> (shape, OCPUs, memory GB, architecture)
SIZES = {
    "arm-2x12": ("VM.Standard.A1.Flex", 2, 12, "arm64"),
    "arm-1x6": ("VM.Standard.A1.Flex", 1, 6, "arm64"),
    "x86-1x8": ("VM.Standard.E5.Flex", 1, 8, "x86_64"),
    "x86-2x8": ("VM.Standard.E5.Flex", 2, 8, "x86_64"),
    "x86-micro": ("VM.Standard.E2.1.Micro", 1, 1, "x86_64"),
}
DEFAULT_SIZE = "arm-2x12"

MANIFEST = dict(BASE, name="oci", place="oci", label="Oracle Cloud",
                # Stop and start: the boot volume keeps everything; running programs don't survive a stop.
                sleep_wake={"kind": "stop-start", "keeps_processes": False},
                sizes=tuple(SIZES),
                # The computer's own network security group (outside the guest): egress only to public addresses,
                # or none with EGRESS=off. OCI doesn't apply security rules to 169.254.0.0/16, so its metadata
                # service (and its DNS) can't be refused from outside: only the guest's firewall refuses them.
                outside_egress={"enforced": True, "private_ranges": True, "metadata": False, "off": True},
                # Stopped through the API, a standard shape bills only its boot volume. A guest that powers itself
                # off (the idle stop) still counts as running, and bills, until it's stopped through the API.
                billing={"stopped": "storage", "guest_poweroff": "nothing",
                         "note": "stopped through the API: only the boot volume is billed (standard shapes); a guest "
                                 "that powered itself off is still billed until metacomputer stop"})

# What the command line needs from it (lib/cloud/manifests.py says what each is).
CLI = {
    "owners": ("@jonradoff",),
    "place_words": "oci (Oracle Cloud)",
    "credentials": ENV_KEYS,
    "not_secret": ("OCI_REGION",),
    "credential_words": "OCI_*",
    "ask_needs": ("OCI_USER", "OCI_API_KEY_PEM"),
    "python": "OCI_PYTHON",
    "words": {
        "vm_create_confirm": "This makes the instance on Oracle Cloud, then installs the desktop (about 10 minutes).",
    },
}
