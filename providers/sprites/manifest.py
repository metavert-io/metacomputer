"""Fly Sprites' manifest: what it has (lib/cloud/capabilities.py; providers/sprites/adapter.py says what a Sprite is)."""
from cloud.ubuntu_vm import BASE

MANIFEST = dict(
    BASE, name="sprites", place="sprites", label="Fly Sprites",
    root_disk="persistent",
    # Checkpoints of the whole disk (copy-on-write): baseline, reset and named checkpoints. A restore restarts the
    # Sprite's programs (open apps close; the desktop comes back) and cuts its connections.
    snapshots={"keep": None, "what": "checkpoints of the whole disk, copy-on-write; a restore restarts its programs"},
    # Asleep about 10 s after its last connection closes; awake again at the next one, its programs as they were.
    sleep_wake={"kind": "auto-sleep", "keeps_processes": True},
    # Fly's network policy (DNS names), enforced outside the VM: an allowlist with `net allow`.
    egress={"model": "dns-allowlist", "allowlist": True, "private_ranges_blocked": True},
    # Enforced outside the VM (nothing in it, root included, can change it). While a policy is in force Fly refuses
    # raw IP addresses (an address is let through only after an allowed name resolved to it), private ranges and the
    # metadata service, even through a name that resolves to one (checked live: 10/8, 172.16/12, 192.168/16,
    # 100.64/10, 169.254.169.254, Fly's private network); EGRESS=off denies every name.
    outside_egress={"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    # Asleep, a Sprite bills only its disk (cold storage); awake, the CPU time it uses, its memory and its disk.
    billing={"stopped": "storage", "guest_poweroff": "n/a",
             "note": "asleep (about 10 s after nothing is connected): only its disk, at Fly's storage rate; awake: the "
                     "CPU time it uses, the memory in use and its disk"},
    # The Sprite's kernel has no uinput, so the driver has no virtual devices: keys, typing and pixel clicks go in
    # the foreground (XTEST); clicks and actions on an element go through accessibility, as anywhere.
    input={"background": False, "note": "no /dev/uinput on a Sprite: keys, typing and pixel clicks go in the "
                                        "foreground; actions on an element through accessibility"},
    sizes=(), regions=(), fresh_start=False)

# What the command line needs from it (lib/cloud/manifests.py says what each is). Fly's sprite command keeps its own
# sign-in: there are no credentials in the environment (providers/sprites/cli.sh pv_sprites_can_ask says whether the
# command is there).
CLI = {
    "owners": ("@jonradoff",),
    "place_words": "sprites (Fly Sprites)",
    "docs": "docs/sprites.md",
    "credential_words": "the sprite command's sign-in",
    "transport": "sprite",
    "transport_settings": ("SPRITE_ORG", "SPRITE_NAME", "SPRITE_CLI"),
    # (OPTION, WHAT IT NEEDS, ITS DEFAULT FROM THE ENVIRONMENT, ONLY FOR THIS PROVIDER)
    "create_options": (("--org", "the Fly organization's name", "SPRITES_ORG", False),
                       ("--sprite", "the Sprite's name", "", True)),
    "cloud_options": (("--org", "SPRITES_ORG"),),
    "teardown": False,
    "words": {
        "vm_create_confirm": "This makes the Sprite on Fly, then installs the desktop (about 5 minutes).",
        "instance_word": "Sprite",
        "status_baseline": "a checkpoint at Fly (metacomputer checkpoints)",
        "status_network": "Fly's network policy: EGRESS={egress} (net status reads what's in force), SSH only through "
                          "Fly's tunnel   sleep: by itself about 10 s after nothing is connected",
        "status_uinput": "  (none on a Sprite: foreground input)",
    },
}
