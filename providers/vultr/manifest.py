"""Vultr's manifest: what it has (lib/cloud/capabilities.py), its sizes and regions, and the environment variable
its API token comes in (providers/vultr/adapter.py says what each is)."""
from cloud.vps import manifest

ENV_KEY = "VULTR_API_KEY"
# size -> (architecture, vCPUs, memory GB, disk GB, USD an hour): Vultr's list prices, read from its API on
# 2026-11-23; billed by the hour up to the monthly price (vc2-2c-4gb $20, vc2-4c-8gb $40).
SIZES = {
    "vc2-2c-4gb": ("x86_64", 2, 4, 80, 0.027),
    "vc2-4c-8gb": ("x86_64", 4, 8, 160, 0.055),
}
REGIONS = ("ewr", "ord", "dfw", "sea", "lax", "atl", "ams", "lhr")

MANIFEST = manifest(
    "vultr", "Vultr", SIZES, REGIONS,
    # Vultr's firewall groups filter only what comes in (SSH, here): nothing outside the computer holds its egress.
    {"enforced": False, "private_ranges": False, "metadata": False, "off": False},
    "billed by the hour up to the monthly price; a stopped instance is billed in full: only deleting it "
    "(metacomputer vm delete) stops the charges, and the idle stop saves nothing")

# What the command line needs from it (lib/cloud/manifests.py says what each is). Its idle stop is off unless asked:
# it saves nothing there, and Vultr doesn't see a guest power itself off (it still counts it as running, and its start
# would restart a running one).
CLI = {
    "owners": ("@jonradoff",),
    "credentials": (ENV_KEY,),
    "api_host": "api.vultr.com",
    "idle_stop_default": "off",
}
