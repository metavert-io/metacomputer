# Third-party notices

## Included in this repository and its Claude Code plugin

**Cua Driver skill pack** (`.claude/skills/cua-driver/`, also in the plugin as
`metacomputer:cua-driver`), from the [trycua/cua](https://github.com/trycua/cua)
release `cua-driver-rs-v0.53.0`. It is trimmed to the Linux guide; `SKILL.md`,
`README.md` and `RUNTIME.md` are edited for this project, where the driver runs
only inside the VM (`scripts/vendor-driver-skill.py` records exactly how). Its
`LICENSE` file holds the notice below, taken from the same release.

```text
MIT License

Copyright (c) 2025 Cua AI, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Downloaded or built at install time (not included)

| Component | Source | License |
| --- | --- | --- |
| Lume (VM runner) | trycua/cua release `lume-v0.7.2`, checksum pinned; or built from that tag by mcomp-vm | MIT, © Cua AI, Inc. |
| mcomp-vm (metacomputer's build of Lume, formerly lume-secure; made by Metavert, not by Cua) | github.com/metavert-io/metacomputer-vm (patches and build scripts), tag and commit pinned | MIT, © Metavert LLC |
| Cua Driver (in the VM) | trycua/cua release `cua-driver-rs-v0.53.0`, checksum pinned | MIT, © Cua AI, Inc. |
| rclone (on the Mac, only for S3 volumes; downloaded on first use) | rclone/rclone release `v1.75.2`, `SHA256SUMS` signature and zip checksum pinned | MIT, © Nick Craig-Wood |
| cua-netgw (network gateway), built as mcomp-netgw | built from trycua/cua source at a pinned commit (`libs/cua/netgw`), modified by metacomputer's patches in `patches/netgw/` (and named mcomp-netgw because it's modified) | MIT, © Cua AI, Inc.; uses gvisor-tap-vsock (Apache-2.0) |
| go-qcow2reader (used by `tools/qcow2raw`) | github.com/lima-vm/go-qcow2reader v0.8.0, fetched by Go | Apache-2.0 |
| Ubuntu 24.04 cloud image and packages | cloud-images.ubuntu.com, signature checked | Ubuntu's licenses |
| Firefox | Mozilla's APT repository, key pinned | MPL-2.0 |
| Google Chrome (optional) | Google's APT repository, key pinned | Google Chrome Terms of Service: installing it means you accept them |
