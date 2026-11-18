# Storage

The virtual computer is disposable: `reset` puts back the clean baseline, and nothing
in the VM is shared with your Mac. Storage is how files outlive a reset, travel
between VMs, or come from your Mac, while the VM stays untrusted. Every kind is off
until you attach it, and lives outside the VM, so `reset`, `restore` and
`build --replace` keep it.

| Kind | What it is | In the VM | Shared with |
| --- | --- | --- | --- |
| **Disk volume** | An ext4 disk image on the Mac (`volume create NAME --size 20G`) | Read-write, at `~/data/NAME` by default | One VM at a time (locked while it runs) |
| **Reference disk** | A read-only image packed from a Mac folder (`refdisk create NAME --from FOLDER`) | Read-only, at `~/ref/NAME` by default | Any number of VMs |

To copy a single file in without keeping it anywhere, use `metacomputer put`
([below](#copying-files-in-put)).

[SECURITY.md](../SECURITY.md#storage) has what each kind lets a hostile VM do, and what
protects the Mac.

## Quick start

```bash
metacomputer volume create work --size 20G      # a sparse image: it takes space only as it fills
echo 'VOLUMES=work' >> config/local.env         # or edit the file
metacomputer restart                            # attached at start; formatted inside the VM on first use
metacomputer sh -c 'echo hello > ~/data/work/hello.txt'
metacomputer reset --yes                        # the VM goes back to its baseline ...
metacomputer sh -c 'cat ~/data/work/hello.txt'  # ... and the volume's files are still there
```

## Disk volumes

```bash
metacomputer volume create NAME --size 20G [--dir PATH]
metacomputer volume list [--json]
metacomputer volume resize NAME --size 40G      # grow only; the filesystem grows at the next start
metacomputer volume delete NAME [--yes]         # asks first; its checkpoint snapshots go too
```

- **The image.** A raw disk image (`NAME.img`), sparse, created on the Mac. It's
  attached to the VM as a read-write virtio disk (`lume run --disk`).
- **Formatting happens inside the VM, only on a blank disk.** On first use the VM's
  helper checks that the disk has no filesystem or partition signature and that its
  first and last MiB read as zeros, then formats it ext4 with the volume's own UUID
  (chosen on the Mac when it was created). A disk with any other filesystem, or ext4
  with another UUID, is refused: never formatted, never mounted.
- **Mounted** `nodev,nosuid,noatime`, owned by the desktop user. Before each mount
  the VM runs `e2fsck -p`; a filesystem it can't repair is refused.
- **One VM at a time.** A lock, held by the VM's own process while it runs, keeps
  another VM (from this checkout or any other on this Mac) from attaching the same
  image; that VM's `start` refuses and names the holder. A lock whose process is gone
  (the VM crashed) no longer counts. A small `NAME.img.lock` file beside the image
  names this computer, so another Mac reading the same drive refuses too, but see
  the warning about network filesystems below.
- **Survives** `reset`, `restore`, `baseline` and `build --replace`.
- **Checkpoints.** `metacomputer checkpoint NAME --with-volumes` also snapshots every
  disk volume in `VOLUMES` (an APFS clone when the image is on APFS, which is instant
  and takes no space until either copy changes; a copy otherwise).
  `metacomputer restore NAME` then rolls those volumes back with the VM, and says so
  before it asks. Without `--with-volumes`, checkpoints leave volumes alone.
  `checkpoints` lists which volumes each one holds; `checkpoint --delete` removes
  their snapshots too.

## Reference disks

```bash
metacomputer refdisk create NAME --from FOLDER [--dir PATH]
metacomputer refdisk list [--json]
metacomputer refdisk delete NAME [--yes]
```

`create` packs a copy of the folder into a hybrid UDF and ISO 9660 image
(`hdiutil makehybrid`) and makes the file read-only. In the VM it mounts as UDF:
long names (tested with 150 characters), non-ASCII names and the permission bits,
including the executable bit, come through. Links and special files are left out (it
says how many): macOS writes UDF links that Linux can't read, and a link could point
outside the folder. The folder is checked against the same list of private folders as
anything else that goes into the VM ([below](#what-may-not-go-into-the-vm)).

It's read-only at the hypervisor: the first reference disk is attached with
`lume run --mount`, the others as USB storage, both read-only, and the VM mounts them
read-only, `nodev,nosuid`. So any number of VMs can attach the same one at once, and
none can change it. To update one, delete it and create it again.

## Attaching: VOLUMES and REFDISKS

In `config/local.env` (or the file `MCOMP_CONFIG` names):

```bash
VOLUMES="work"                          # ~/data/work in the VM
VOLUMES="work:/volume,cache:/mnt/cache" # your own mount points, separated by commas
REFDISKS="docs, models:/data/models"    # ~/ref/docs, /data/models
```

Each entry is `name[:mountpoint]`. They're attached at the next `start` (or
`restart`); a VM that's already running keeps what it started with.

**Mount points** in the VM: `/volume`, `/mnt`, `/data`, `/srv`, `/workspace`, any
folder in them, or any folder in the desktop user's home (`~/...`, not a dot-folder
and not the home folder itself). So scripts written for Cua Spaces' `/volume` or
`/mnt/shared` keep working. System folders are refused, and so are mount points that
overlap. In the VM, every part of the path is opened without following links, and
missing folders are made (as the desktop user's under its home), so a link planted in
the VM can't redirect the mount.

**Nothing half-attached.** Before the VM boots, `start` checks everything: each item
exists (an image on a drive that isn't connected refuses the start, saying which and
where), it isn't locked by another VM, the mount points are allowed, and the VM was
built with storage support. Then the VM boots with the disks attached and mounts all
of them or none; if anything fails, `start` stops the VM again and says why.

- `metacomputer start --no-volumes` starts with nothing attached: a clean run.
- In bounded mode (`DRIVER_MODE=bounded`), an item is attached only when the driver's
  manifest lists its mount point under `resources.files` (read-only items under
  `read` or `write`, read-write ones under `write`); the rest are left out, and
  `start` says which. The default profiles list none of them.

## Where it's kept

- `STORAGE_DIR` (default `~/.local/share/metacomputer`) is where new volumes
  (`volumes/`) and reference disks (`refdisks/`) go. `--dir PATH` on `create` puts one
  item elsewhere. Any folder you can reach works: the internal disk, an external
  drive, an SMB or NFS mount. Nothing assumes a particular machine's drives.
- Each item remembers its own path. The list of items and the locks stay on this
  Mac's internal disk (`~/.local/share/metacomputer/storage/`), so `volume list`
  still works, and says "missing", while a drive is disconnected.
- **Network filesystems.** A disk volume on SMB, NFS, AFP or WebDAV (detected with
  statfs) gets a warning when it's created and at every start: if the connection
  drops while the VM writes, the volume can be damaged, and locks don't hold across
  computers. Prefer a local or directly attached drive for read-write volumes.

## What may not go into the VM

A folder packed into a reference disk is refused when it is, holds, or is inside one
of these (links are resolved first, so a link to one is refused too):
- your home folder itself, and Documents, Desktop, Downloads, Pictures, Movies, Music
  and Public themselves (a folder inside Documents is allowed), or any parent of them;
- anything in `~/Library` (iCloud Drive is `~/Library/Mobile Documents`, other cloud
  drives `~/Library/CloudStorage`), in a dot-folder of your home (`.ssh`, `.aws`,
  `.config`, ...) or in `SSH_DIR`;
- this metacomputer checkout, Lume's VMs (`~/.lume`), `STORAGE_DIR` itself and the
  storage registry;
- `/`, `/Volumes` itself, `/Users`, `/tmp` itself and the gateway's socket folders
  there, `/System`, `/Library`, `/Applications`, `/usr`, `/bin`, `/sbin`, `/etc`, `/var`,
  `/dev` and Homebrew.

## Copying files in: put

`metacomputer put HOST_PATH... [--to GUEST_DIR] [--overwrite]` copies files and
folders into the VM (default `~/Inbox`) over the same tar-over-SSH transport as
before (no Mac metadata or extended attributes; files owned by the desktop user):
- each item lands first in a hidden folder inside `GUEST_DIR`;
- every regular file is hashed (SHA-256) on the Mac before it's sent and again in the
  VM; any difference, missing file or extra file, and nothing is put in place;
- a name that's already taken is never overwritten: the copy lands as `name (1).ext`,
  `name (2).ext` and so on (folders as `name (1)`). `--overwrite` replaces the
  existing file or folder instead.

It prints each item's final path in the VM, and how many files were verified.

## VMs built before storage support

Storage needs the VM's side (a helper and a `metacomputer-admin storage` verb), which
only a build installs. With `VOLUMES` or `REFDISKS` set, a VM built before it refuses
to start, before it boots, and says to rebuild it (`metacomputer build --replace`:
volumes and reference disks are outside the VM and are kept) or to start it with
`--no-volumes`. `metacomputer status` says so too. The Mac knows from
`SSH_DIR/guest-features`, which a build writes when it finishes, and the VM's helper
is asked again before anything is mounted.

## Seeing what's attached

- `metacomputer status` lists every attachment: kind, name, mount point, `rw` or `ro`,
  the Mac path, and whether the VM has it mounted.
- `metacomputer security` lists what each attachment lets the VM do.
- `metacomputer volume list` and `refdisk list` show every item, where it is, and
  which VM holds each volume.
- With an event sink, `start` sends `lifecycle.storage_attached` with the list.

## Coming from Cua Volume

[Cua Volume](https://cua.ai/docs/spaces/guides/cua-volume) is Cua Spaces' storage: one
versioned drive per account that every Space and agent shares, with access rules,
grants, an audit log and an optional S3 backend. metacomputer has no daemon serving a
drive; it attaches plain disks to one VM on your Mac. The closest equivalents:

| Cua Volume | metacomputer |
| --- | --- |
| Named volume: `cua volume named create shared` | `metacomputer volume create shared --size 20G` (a disk image of a fixed size; `volume resize` grows it) |
| Mount it: `cua spaces create --volume shared:/mnt/shared` | `VOLUMES=shared:/mnt/shared` in the config, then `start` |
| Read-only mount: `--volume shared:/mnt/shared:ro`, any number of Spaces | A reference disk (`refdisk create NAME --from FOLDER`, `REFDISKS=NAME:/mnt/shared`): read-only, any number of VMs |
| A Space's `/volume` | `VOLUMES=NAME:/volume` |
| Accepted mount points: `/mnt`, `/data`, `/srv`, `/workspace`, folders the user owns | The same, plus `/volume` |
| `cua volume named ls` / `rm` (refused while mounted) | `volume list` / `volume delete` (refused while a VM holds it) |
| Files outlive the Space | Volumes outlive `reset`, `restore` and `build --replace` |
| Restore a file from its history | `checkpoint NAME --with-volumes` / `restore NAME`: the whole volume, at the moments you chose |
| `send_file`: SHA-256 verified, `name (1).ext` on a collision, `conflict: "overwrite"` | `metacomputer put`: the same rules; `--overwrite` |
| Storage on this computer (`~/.cua/volume/data`) | `STORAGE_DIR` (default `~/.local/share/metacomputer`), or `--dir` per item, on any drive |

What isn't offered:
- **Per-write versioning** and restoring one file: checkpoints snapshot whole volumes,
  when you take them.
- **Per-agent and per-Space grants, requests and approvals:** a volume is attached to
  one VM, and everything in that VM can use all of it.
- **A secret scanner** on writes: don't put credentials on a volume; keep them in the
  vault (`metacomputer secrets`).
- **One drive mounted by many VMs read-write:** a disk volume belongs to one VM at a
  time; reference disks are read-only.
- **An audit log of reads and writes** in the volume: the event stream records what
  was attached, not what the VM did with it.

## How it works

- `lib/storage.py` keeps the registry and the locks, checks the plan for a start, and
  writes the extra `lume run` arguments (`--disk` for each volume, in order; `--mount`
  for the first reference disk, `--usb-storage` for the rest).
- In the VM, `/opt/metacomputer/bin/storage-helper` (root-owned, run only through
  `sudo metacomputer-admin storage attach`) reads the plan on stdin. It finds each
  volume among the extra read-write virtio disks by its place in that order and checks
  its size against the image's; each reference disk by its volume label. It mounts
  through descriptors opened without following links, undoes everything if one item
  fails, and records what it mounted in `/run/metacomputer/storage.json`, which
  `guest-status` reports.
- The lock files are in `~/.local/share/metacomputer/storage/locks/`; each names the VM,
  its checkout and its process (with the process's start time, so a reused pid doesn't
  count). `start` takes them before the VM boots, and `stop` (also `reset`, `restore`
  and `baseline`) lets go of them after it's off.
