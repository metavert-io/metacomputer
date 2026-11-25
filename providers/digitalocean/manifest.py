"""DigitalOcean's manifest: what it has (lib/cloud/capabilities.py), its sizes and regions, and the environment variable
its API token comes in (providers/digitalocean/adapter.py says what each is)."""
from cloud.vps import manifest

ENV_KEY = "DIGITALOCEAN_TOKEN"
# size -> (architecture, vCPUs, memory GB, disk GB, USD an hour): DigitalOcean's list prices, read from its API on
# 2026-11-23; billed by the hour up to the monthly price (s-2vcpu-4gb $24, s-4vcpu-8gb $48).
SIZES = {
    "s-2vcpu-4gb": ("x86_64", 2, 4, 80, 0.03571),
    "s-4vcpu-8gb": ("x86_64", 4, 8, 160, 0.07143),
}
REGIONS = ("nyc3", "sfo2", "tor1", "lon1", "ams3", "sgp1", "syd1")

MANIFEST = manifest(
    "digitalocean", "DigitalOcean", SIZES, REGIONS,
    # Its own Cloud Firewall (outside the guest): out, public IPv4 addresses only (and DNS to its VPC's resolver), or
    # nothing at all. The metadata service answers through it (tested live): only the guest refuses it.
    {"enforced": True, "private_ranges": True, "metadata": False, "off": True},
    "billed by the hour up to the monthly price; a powered-off Droplet is billed in full: only deleting it "
    "(metacomputer vm delete) stops the charges, and the idle stop saves nothing")

# What the command line needs from it (lib/cloud/manifests.py says what each is).
CLI = {
    "owners": ("@jonradoff",),
    "credentials": (ENV_KEY,),
    "api_host": "api.digitalocean.com",
}
