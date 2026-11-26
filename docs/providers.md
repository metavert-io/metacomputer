# Providers

<!-- Made from providers/<name>/manifest.py by scripts/gen-providers.py: don't edit by hand (its --check, run by tests/test_cloud_adapters.py, fails a copy that isn't current). -->

Where metacomputer can make a computer besides this Mac and your own server (docs/remote.md). Each provider is a folder of its own, `providers/<name>/` ([providers/README.md](../providers/README.md)), listed in `lib/cloud/registry.py`. What each one has is its manifest's (`lib/cloud/capabilities.py` says what the fields mean); its docs page says how to set it up and what keeps it apart.

| Provider | `--on` | Sleep | PERSIST | Snapshots | Egress | Outside the computer | Stopped bills | Background input | Credentials | Docs | Code |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Oracle Cloud | `oci` | `stop-start` | disk | no | `guest-firewall` | private ranges refused, can be turned off | `storage` | yes | `OCI_USER`, `OCI_TENANCY`, `OCI_FINGERPRINT`, `OCI_REGION`, `OCI_API_KEY_PEM`, `OCI_COMPARTMENT` | [docs/remote.md](remote.md) | [providers/oci/](../providers/oci/) |
| Fly Sprites | `sprites` | `auto-sleep` | disk | yes | `dns-allowlist` | private ranges refused, metadata refused, can be turned off | `storage` | no (foreground) | the sprite command's sign-in | [docs/sprites.md](sprites.md) | [providers/sprites/](../providers/sprites/) |
| Hetzner Cloud | `hetzner` | `stop-start` | disk | no | `guest-firewall` | private ranges refused, can be turned off | `full` | yes | `HCLOUD_TOKEN` | [docs/remote.md](remote.md) | [providers/hetzner/](../providers/hetzner/) |
| DigitalOcean | `digitalocean` | `stop-start` | disk | no | `guest-firewall` | private ranges refused, can be turned off | `full` | yes | `DIGITALOCEAN_TOKEN` | [docs/remote.md](remote.md) | [providers/digitalocean/](../providers/digitalocean/) |
| Vultr | `vultr` | `stop-start` | disk | no | `guest-firewall` | none (only its own firewall) | `full` | yes | `VULTR_API_KEY` | [docs/remote.md](remote.md) | [providers/vultr/](../providers/vultr/) |
| Vercel Sandbox | `vercel` | `stop-start` | disk | yes | `network-policy` | private ranges refused, metadata refused, can be turned off | `storage` | no (foreground) | `VERCEL_TOKEN`, `VERCEL_TEAM_ID`, `VERCEL_PROJECT_ID` | [docs/vercel.md](vercel.md) | [providers/vercel/](../providers/vercel/) |
| Cloudflare Containers | `cloudflare` | `stop-start` | disk | yes | `http-intercept` | private ranges refused, metadata refused, can be turned off | `storage` | no (foreground) | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | [docs/cloudflare.md](cloudflare.md) | [providers/cloudflare/](../providers/cloudflare/) |

What each bills while stopped (`Stopped bills`): `storage` its disk only, `full` everything, as if it ran. `Sleep`: `stop-start` (metacomputer stops and starts it through the provider), `auto-sleep` (it sleeps by itself when nothing is connected).

## Oracle Cloud

- Billing: stopped through the API: only the boot volume is billed (standard shapes); a guest that powered itself off is still billed until metacomputer stop.
- Input: background input (the driver's virtual devices) works there.
- Sizes: `arm-2x12`, `arm-1x6`, `x86-1x8`, `x86-2x8`, `x86-micro`.
- Regions: not chosen per computer.

## Fly Sprites

- Billing: asleep (about 10 s after nothing is connected): only its disk, at Fly's storage rate; awake: the CPU time it uses, the memory in use and its disk.
- Input: no /dev/uinput on a Sprite: keys, typing and pixel clicks go in the foreground; actions on an element through accessibility.
- Sizes: the provider chooses.
- Regions: not chosen per computer.

## Hetzner Cloud

- Billing: billed by the hour up to the monthly price; a stopped server is billed in full (and its IPv4 address): only deleting it (metacomputer vm delete) stops the charges, and the idle stop saves nothing.
- Input: background input (the driver's virtual devices) works there.
- Sizes: `cax11`, `cax21`, `cax31`, `cx23`, `cx33`.
- Regions: `fsn1`, `nbg1`, `hel1`.

## DigitalOcean

- Billing: billed by the hour up to the monthly price; a powered-off Droplet is billed in full: only deleting it (metacomputer vm delete) stops the charges, and the idle stop saves nothing.
- Input: background input (the driver's virtual devices) works there.
- Sizes: `s-2vcpu-4gb`, `s-4vcpu-8gb`.
- Regions: `nyc3`, `sfo2`, `tor1`, `lon1`, `ams3`, `sgp1`, `syd1`.

## Vultr

- Billing: billed by the hour up to the monthly price; a stopped instance is billed in full: only deleting it (metacomputer vm delete) stops the charges, and the idle stop saves nothing.
- Input: background input (the driver's virtual devices) works there.
- Sizes: `vc2-2c-4gb`, `vc2-4c-8gb`.
- Regions: `ewr`, `ord`, `dfw`, `sea`, `lax`, `atl`, `ams`, `lhr`.

## Vercel Sandbox

- Billing: stopped: its snapshot's storage only; running: the CPU time it uses and all of its memory (a session ends at its timeout, IDLE_STOP after it was last used, or at the plan's cap).
- Input: no /dev/uinput in a Vercel sandbox: keys, typing and pixel clicks go in the foreground; actions on an element through accessibility.
- Sizes: `2x4`, `4x8`, `8x16`.
- Regions: `iad1`, `arn1`, `bom1`, `cdg1`, `cle1`, `cpt1`, `dub1`, `fra1`, `gru1`, `hkg1`, `hnd1`, `icn1`, `kix1`, `lhr1`, `pdx1`, `sfo1`, `sin1`, `syd1`, `yul1`.

## Cloudflare Containers

- Billing: stopped: its snapshots' storage, free during Cloudflare's beta; running: its memory and disk as provisioned and the CPU it uses (Workers Paid, $5 a month, includes some).
- Input: no /dev/uinput in a Cloudflare container: keys, typing and pixel clicks go in the foreground; actions on an element through accessibility.
- Sizes: `standard-2`, `standard-3`, `standard-4`.
- Regions: not chosen per computer.
