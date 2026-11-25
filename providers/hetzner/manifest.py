"""Hetzner Cloud's manifest: what it has (lib/cloud/capabilities.py), its sizes and regions, and the environment variable
its API token comes in (providers/hetzner/adapter.py says what each is)."""
from cloud.vps import manifest

ENV_KEY = "HCLOUD_TOKEN"
# size -> (architecture, vCPUs, memory GB, disk GB, USD an hour): Hetzner's list prices for a US-dollar account,
# read from its API on 2026-11-23 (an account billed in euros pays Hetzner's euro prices); each is billed by the hour
# up to its monthly price (cax11 $6.99, cax21 $12.49, cax31 $24.99, cx23 $6.49, cx33 $9.99).
SIZES = {
    "cax11": ("arm64", 2, 4, 40, 0.0112),
    "cax21": ("arm64", 4, 8, 80, 0.0200),
    "cax31": ("arm64", 8, 16, 160, 0.0400),
    "cx23": ("x86_64", 2, 4, 40, 0.0104),
    "cx33": ("x86_64", 4, 8, 80, 0.0160),
}
REGIONS = ("fsn1", "nbg1", "hel1")

MANIFEST = manifest(
    "hetzner", "Hetzner Cloud", SIZES, REGIONS,
    # Its own Cloud Firewall (outside the guest): out, public IPv4 addresses only, or nothing. Hetzner's firewalls
    # don't filter its metadata service (answered locally), DHCP or its DNS resolvers: only the guest refuses those.
    {"enforced": True, "private_ranges": True, "metadata": False, "off": True},
    "billed by the hour up to the monthly price; a stopped server is billed in full (and its IPv4 address): only "
    "deleting it (metacomputer vm delete) stops the charges, and the idle stop saves nothing")

# What the command line needs from it (lib/cloud/manifests.py says what each is).
CLI = {
    "owners": ("@jonradoff",),
    "credentials": (ENV_KEY,),
    "api_host": "api.hetzner.cloud",
}
