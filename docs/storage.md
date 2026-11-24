# Storage

The virtual computer is disposable: `reset` puts back the clean baseline, and nothing
in the VM is shared with your Mac. Storage is how files outlive a reset, travel
between VMs, or come from your Mac, while the VM stays untrusted. Every kind is off
until you attach it, and lives outside the VM, so `reset`, `restore` and
`build --replace` keep it.

| Kind | What it is | In the VM | Shared with |
| --- | --- | --- | --- |
| **Disk volume** | An ext4 disk image on the Mac (`volume create NAME --size 20G`) | Read-write, at `~/data/NAME` by default | One VM at a time (locked while it runs) |
| **Folder volume** | A folder on your Mac, shared live (`volume create NAME --folder PATH`); it's what makes the VM's files visible in Finder | Read-only by default; read-write only with `:rw` | Read-only: any number of VMs and the Mac. Read-write: one VM at a time |
| **S3 volume** | A folder volume whose folder is a local copy of a bucket, kept in step with `rclone bisync` (`volume create NAME --s3 ...`) | Like a folder volume | Like a folder volume, and through the bucket with other computers |
| **Reference disk** | A read-only image packed from a Mac folder (`refdisk create NAME --from FOLDER`) | Read-only, at `~/ref/NAME` by default | Any number of VMs |

To copy a single file in without keeping it anywhere, use `metacomputer put`
([below](#copying-files-in-put)).

[SECURITY.md](../SECURITY.md#storage) has what each kind lets a hostile VM do, and what
protects the Mac.

## Quick start

With a VM built by this version (an older one needs `metacomputer build --replace` once;
[below](#vms-built-before-storage-support)):

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
metacomputer volume delete NAME [--yes] [--keep-image]   # asks first; its checkpoint snapshots go too
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
  image; that VM's `start` refuses and names the holder. A lock counts until its process
  is certainly gone (the VM crashed): one whose process can't be told alive or gone still counts. A small `NAME.img.lock` file beside the image
  names this computer, so another Mac reading the same drive refuses too, but see
  the warning about network filesystems below.
- **Its folder stays out of every VM.** The folder holding an image (and its checkpoint
  snapshots, which sit beside it) can't be shared into a VM, nor can any parent of it.
  Where a new image goes gets the same checks whether it's `--dir` or `STORAGE_DIR`: not
  inside a folder volume, and not inside a folder any VM has read-write (the new image is
  reserved before anything is made, so a VM can't attach such a folder meanwhile either). A
  VM that could write there could replace the image, or a folder above it, with a link to
  any file on the Mac just before a start or a checkpoint read it.
- **Bound to the file that was made.** An image's path is recorded exactly as the kernel
  spells it (firmlinks mapped back, its case and Unicode form), so every comparison with
  another item's path sees one spelling, together with the image's device and inode. Every
  later use (start, checkpoint, restore, resize, delete) walks that path from `/` one folder
  at a time, each opened without following a link, and checks the file at the end is still
  that image and still has that spelling: a folder in the path replaced by a link, the image
  replaced by another file, or a record in any other spelling (such as
  `/System/Volumes/Data/Users/...`) is refused, never followed. There's no re-binding an
  item in place: one without a recorded identity (made by an earlier build of this
  feature) is refused with the way out, delete it and create it again.
- **Deleting.** `volume delete NAME` deletes the image, its checkpoint snapshots and the
  files on it (copy out what you need first). With `--keep-image` the image file stays where
  it is, no longer a volume and no longer protected; it can't be attached again (create a
  new volume instead).
- **Survives** `reset`, `restore`, `baseline` and `build --replace`.
- **Checkpoints.** `metacomputer checkpoint NAME --with-volumes` also snapshots every
  disk volume in `VOLUMES` (an APFS clone when the image is on APFS, which is instant
  and takes no space until either copy changes; a copy otherwise).
  `metacomputer restore NAME` then rolls those volumes back with the VM, and says so
  before it asks. Without `--with-volumes`, checkpoints leave volumes alone.
  `checkpoints` lists which volumes each one holds; `checkpoint --delete` removes
  their snapshots too. Saving, restoring and deleting a checkpoint each take the
  checkpoint's lock first, then read its manifest (which nothing else can change now), then
  hold exactly the volumes it names until every change is made (registry entries
  included), so they take turns, and no VM can attach a volume meanwhile; a restore checks
  that each volume is still the one in the checkpoint before it changes anything. A
  `volume delete` takes the locks of the checkpoints that hold the volume too. Snapshots are copied from the
  image's own descriptor (a link in its place is refused), sparse where they aren't clones,
  never past the size that was checked, and only with the free-space reserve left.

## Folder volumes

```bash
metacomputer volume create NAME --folder PATH     # PATH is made if only its last part is missing
VOLUMES="NAME"                                    # read-only, at ~/data/NAME
VOLUMES="NAME:/mnt/shared:rw"                     # read-write: an explicit opt-in
```

A folder volume is a folder on your Mac, shared into the VM over virtiofs
(`lume run --shared-dir PATH:ro|rw`). Both sides see the same files at once, and the
folder stays an ordinary folder in Finder. `volume delete` only forgets it: the folder
and its files stay.

**Read-only** (the default): the VM can read the folder and nothing else. The Mac
holds it read-only; the VM's own read-only mount is a second layer. At every start the
VM's helper, as root and through the raw virtiofs mount (which the VM could remount
however it liked), tries to create a file in each read-only folder, and refuses to
mount the folder if the Mac lets it.

**Read-write** (`:rw`) is for handing work back and forth. It's an explicit opt-in, and:
- **One VM at a time.** A lock, like a disk volume's. Between what VMs and running
  operations (a checkpoint, a sync) hold, the rules are symmetric, so they don't depend on
  which came first, and they hold between the items of one start too: a read-write folder
  can't overlap another read-write folder, can't hold a folder anyone else has (even
  read-only: it could replace that folder with a link to anywhere just as the other VM
  attaches it), and can't hold a disk image or reference disk anything is using. Other
  VMs may still have the same folder read-only. A folder holding images can't be shared
  at all ([disk volumes](#disk-volumes)).
- **Spotlight stays out.** The folder's name, or a parent's, must end in `.noindex`
  (for example `~/vm-share.noindex`; Finder shows the name like that). Spotlight
  doesn't index such folders, so its importers never parse files the VM wrote. Measured
  on macOS 27: a `.noindex` folder (or any folder inside one) wasn't indexed, while a
  `.metadata_never_index` file in a folder no longer kept that folder out, and neither
  did `.metadata_never_index_unless_rootfs`. `mdutil` works only on whole volumes.
  Without `.noindex`, a read-write attach is refused, with the rename to make.
- **A watcher on the Mac** (`lib/storage.py watch`) starts together with the VM's process,
  before the VM is asked anything, and `start` stops the VM if it doesn't. It goes over
  the folder every 2 seconds (longer for big folders: five times as long as a pass takes)
  and compares it with what was there when the VM booted. It works through descriptors:
  the folder is opened once and checked to be the folder that was checked, every
  subfolder is opened without following links and checked to be the entry it listed, and
  every change is made relative to those descriptors, so a folder the VM swaps for a link
  in the middle of a pass leads nowhere:
  - every new or changed file and folder gets macOS's quarantine attribute
    (`com.apple.quarantine`, `0081;<time>;metacomputer;`), so Gatekeeper checks it like
    a download, and it's checked again through the file's own descriptor on every pass
    (not only when the file looks changed), so an attribute the VM strips right after it
    was set comes back on the next pass;
  - a symbolic link is kept only when its target, read as text, can't lead out of the
    folder: a relative target whose `..` steps all come first, no more of them than the
    link is deep, then names going down (`../lib/x`, `sub/file`, `.`). Any other link is
    replaced by a short note, `NAME.link-removed.txt`, saying where it pointed: an
    absolute target (the Mac's paths aren't the VM's, and `FOLDER/../..` names a way
    out), or a `..` after a name (with `a -> .`, the link `a/../outside` climbs out,
    however each link looks on its own). The rule holds for chains of links because the
    kernel resolves `..` from the real folder it has reached;
  - a FIFO, socket or device is removed, and set-user-ID and set-group-ID bits come off;
  - each of these sends a `security` event (`symlink_neutralized`,
    `special_file_removed`, `quarantine_restored`); `status`, `security` and `stop`
    report the counts.
  What was in the folder before the VM booted is left alone, so your own files and
  links stay as they were. Files you change in the folder while the VM runs are
  treated as the VM's (the watcher can't tell who wrote them) and get the attribute too.
- **When it can't protect the folder, the VM stops.** More than 500,000 files and folders
  (counted as each folder is listed, so one with millions of entries fails at the limit
  instead of being read whole), folders nested more than 128 deep, a pass that takes more
  than 5 minutes, an attribute that won't stick, an error the watcher didn't expect, or
  the Mac's disk with less free space than `IMPORT_MIN_FREE_GB` (10 GB by default: a
  read-write folder could otherwise fill it) end the VM's process at once, from the Mac,
  and only then is it reported (`security.storage_protection_failed`), so a report that
  fails, on a full disk say, can't keep the VM running. A folder or file the VM made
  unreadable or unwritable gets its owner's permission bits back so it can be checked. A
  folder already over the limit isn't attached read-write at all.
- **A supervisor watches the watcher.** A separate process, started with it, stops the VM
  if the watcher ends without finishing (even `kill -9`), shows no progress for a minute
  (stuck), or if a read-write folder's disk drops below the reserve: it checks free space
  twice a second, whatever the scans are doing.

What the VM can do through virtiofs, measured on macOS 27 with Apple's virtiofs
(Lume 0.7.2): read and write files (they're owned by your Mac account), create links
pointing anywhere, FIFOs and hard links (inside the folder only), and list, remove
or rewrite any extended attribute, `com.apple.quarantine` included. It couldn't set
the set-user-ID bit (the Mac dropped it). That's why the watcher
enforces the exact quarantine value rather than only adding a missing one: a value
rewritten with the "user approved" flag (`00c1;…`) let an unsigned binary run on the
Mac, where ours had Gatekeeper kill it (in later runs Gatekeeper held such a binary
instead, which can come with a dialog on the Mac's screen).

**Where it ends:**
- The watcher works after the fact: there's a gap of up to a few seconds between the
  VM writing something and the watcher seeing it. A program on the Mac that follows
  a new link or opens a new file inside that gap isn't protected. And macOS remembers
  a binary it let run: if one ran while the VM had marked it "approved", putting the
  attribute back doesn't stop that same binary later.
- Quarantine makes Gatekeeper check apps and programs opened from Finder or run
  directly. It doesn't stop a script being fed to an interpreter (`sh file`,
  `python3 file`), and documents get only whatever checks the app that opens them
  makes.
- The VM can set other extended attributes (Finder tags, custom icons and the like) on
  files in a read-write folder; only quarantine is enforced.
- Only the watcher and its supervisor stand between the VM and the folder's files while
  it runs. Either stops the VM when the other dies; kill both at once and nothing does
  (`status` says so in red).
- The free-space check is a poll, twice a second: to get past it, the VM would have to
  write more than the reserve (10 GB by default) within half a second, far more than
  virtiofs carries. It isn't a quota, though: for a hard limit, put the folder on an APFS
  volume with a quota (`diskutil apfs addVolume ... -quota`) or on a disk image of its
  own size.

## S3 volumes

```bash
metacomputer volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P] [--dir PATH]
metacomputer volume keys NAME      # in your own terminal: macOS asks for the access key ID and secret
metacomputer volume test NAME      # the endpoint, the keys (list and write), bucket versioning
metacomputer volume sync NAME      # a sync now (also: at start, every S3_SYNC_INTERVAL, at stop)
metacomputer volume sync NAME --resync   # only when you choose: it picks one side wherever both changed
VOLUMES="NAME:/mnt/shared:rw"      # attached like a folder volume
```

An S3 volume is a folder volume whose folder is a local copy, on the Mac, of a bucket
(or of a prefix in it): AWS S3, Cloudflare R2, MinIO or any S3-compatible store. The
local copy (the *shared copy*) is a `.noindex` folder in `STORAGE_DIR/volumes/` (or
`--dir`), visible in Finder. The VM sees only that folder; it never sees the bucket, the
endpoint or the keys. Read-only and read-write work as for any folder volume, with the
same watcher when it's read-write. The local copy may go inside a folder volume (it's
meant to be shared), but not inside a folder a VM has read-write at the moment (it's
reserved before it's made), nor where a VM has a folder, even read-only, that would hold
its staging folder. Every sync checks it's still the folder that was created. One made by an
earlier build, without that record, is refused: `volume delete NAME` removes its keys and
sync state (the bucket and the local copy stay); move the local copy aside, create the volume
again, and its first sync brings the bucket's files back.

- **Two copies; rclone never sees the VM's.** rclone works only on a second, private
  copy, the *sync copy* (`~/.local/share/metacomputer/storage/s3/NAME/sync`, inside
  metacomputer's state folder, which can never be shared into a VM). A sync is three
  steps: the shared copy's changes go to the sync copy; `rclone bisync` runs between the
  sync copy and the bucket; the sync copy's changes come back to the shared copy.
  metacomputer exchanges the two copies itself, through descriptors:
  - the shared copy is opened once, after checking that it may still be shared and that
    its path still leads to the folder that was created (same canonical path, device and
    inode): a copy the VM replaced, or one reached through a parent it replaced, isn't
    synced (the sync fails and says so), never followed;
  - while the sync runs the copy is reserved, so no VM can attach a read-write folder
    around it until it's done;
  - every folder below is opened without following links, and only plain files go
    across: links, FIFOs and devices never do, so a link the VM made to a private file is
    never read;
  - each file is copied from the descriptor that was checked (an APFS clone when both
    copies are on one disk); one going into the shared copy is made in a staging folder
    beside it that no VM sees (`.NAME.mcstage.noindex`, on the same disk; made with the
    volume, exclusively, and bound to its device and inode; it can't be shared, and a sync is
    refused while any VM has a folder, even read-only, that holds it or is inside it),
    given the quarantine attribute through the descriptor metacomputer holds, and
    only then renamed into the shared copy in one step (folders it makes there too): a file
    from the bucket is never visible there without the attribute, not even under a
    temporary name, whether the VM has the volume read-only or read-write, and however the
    sync ended;
  - a change on one side goes to the other; a file changed on both since the last
    exchange keeps both (the newer under its name, the other as `NAME.conflictN.EXT`),
    unless the two have the same contents (compared by SHA-256: the same size and time
    isn't taken as proof); a deletion goes across unless the other side changed the file. Empty folders aren't
    carried, as with rclone's bisync (buckets have no folders).
- **When.** Once before the VM boots (the first time with `--resync`: a file on only one
  side is copied to the other, and a file on both sides that differs keeps the newer copy,
  an older one on the Mac going to the backup folder below), every `S3_SYNC_INTERVAL`
  seconds (default 60) while it runs, and once more at `stop`, after the VM is off, so its
  last writes go up. One VM keeps a volume in sync at a time (a second VM attaching the
  same volume read-only uses the copy without syncing it). `volume list` shows the last
  sync; `volume sync NAME` runs one now.
- **rclone is confined anyway.** Each run goes through `sandbox-exec` with a profile that
  lets it write only in the volume's state folder (which holds the sync copy), nowhere
  else, even where your account could; and read nothing in the home folders, other drives
  or the shared temporary folders but that state folder, its config and itself. Since
  rclone never sees a folder the VM can reach, this guards against rclone itself rather
  than the VM. Without `/usr/bin/sandbox-exec`, rclone isn't run.
- **Space.** Before a sync writes anything, it claims part of the free space of each
  pool it writes to (an APFS container, whose volumes share its free space whatever their
  device numbers; another filesystem on its own): what's free above `IMPORT_MIN_FREE_GB`
  (10 GB by default), less what other syncs running at the same time have claimed, halved
  so another sync still finds some.
  rclone's transfers are capped to that claim (`--max-transfer`), and rclone is stopped if
  the disk falls below the reserve anyway (checked twice a second while it runs, which
  also covers a bucket of millions of empty files); every copy between the two copies is
  checked against the claim and against the disk's real free space, before it starts and
  every 64 MiB as it goes. The sync copy and the
  backups live on this Mac's internal disk (the state folder): with the shared copy there
  too, the copies are APFS clones (no extra space until one changes); with it on another
  drive, the internal disk holds a second full copy. Older versions rclone replaced are
  kept up to `S3_BACKUP_MAX_GB` (5 GB), newest first, pruned before and after each sync. A
  copy past the watcher's entry limit, on either side, fails the sync without bringing
  anything in (and a read-write one isn't attached).
- **Conflicts keep both copies.** A file changed on both sides since the last sync is
  kept twice: the newer one under its name, the other as `name.conflict1.ext` (then
  `conflict2`, ...), on both sides; `volume sync` lists them, and a `task.s3_conflict`
  event names them. A file overwritten or deleted on the Mac's side by a sync is kept
  in `~/.local/share/metacomputer/storage/s3/NAME/backup/` (rclone's `--backup-dir1`);
  on the bucket's side, turn on versioning (`volume test` says whether it's on).
  rclone stops a run in which every file changed, in case something went wrong; for a
  folder of fewer than 50 files (where that's normal) metacomputer runs it again with
  `--force`, and for a larger one the sync fails with an error event and
  `volume sync NAME --force` after you've checked. A failed sync never drops a write: the
  files stay where they are until the next sync succeeds (`task.s3_sync_failed` says so).
- **Resyncs only when you ask.** A resync (rclone's `--resync`) doesn't rename conflicts:
  wherever both sides have a different file it keeps the newer one. So it's never run on
  its own: when rclone says one is needed, the sync fails with an event and the command to
  run (`volume sync NAME --resync`), and the first sync of a local copy that already holds
  files waits for the same command (the first sync of an empty one is just a download).
  A resync keeps the Mac's older copies in the backup folder and, when the volume has a
  prefix, the bucket's older copies in `PREFIX.metacomputer-backup` beside it; with bucket
  versioning on, nothing is lost either way.
- **Keys** go into the macOS login Keychain (service `metacomputer-s3:NAME`, accounts
  `access_key_id` and `secret_access_key`) through `volume keys`, which runs macOS's own
  `security add-generic-password ... -w` prompt in your terminal: a key is never a
  command-line argument, an echo or a config value, and `volume keys` refuses without a
  terminal. For each rclone run they're read from the Keychain into rclone's environment
  only, as an rclone remote defined by environment variables (`RCLONE_CONFIG_MCS3_*`),
  with nothing else of yours in that environment and an empty `rclone.conf`. They never
  go in arguments, rclone.conf, logs, events or the VM, and rclone's output is scrubbed of
  them before it's logged (`storage/s3/NAME/rclone.log`). `volume delete` removes them
  from the Keychain (the bucket and the local copy stay).
- **rclone** is pinned by version and SHA-256 in `config/defaults.env` and downloaded on
  first use by `scripts/fetch-artifacts.sh rclone`, like the build's other artifacts:
  rclone's `SHA256SUMS` must be signed by rclone's release key (fingerprint pinned; only
  that key is kept from the key file), and the pinned digest must match the signed list and
  the zip. The program inside the zip is pinned as well (`RCLONE_BIN_SHA256`): before every
  use the unpacked program is checked against that pinned value, never against a file on
  disk, and one that's missing or different (a planted copy, say) is never run but unpacked
  again from the zip, once the zip matches its own pin (downloaded and verified again if
  needed). It runs only on the Mac.
- **Endpoints:** `https://...`, or `http://` (a warning unless it's this Mac's
  loopback: the keys sign each request but the files travel unencrypted). Use a key
  limited to that bucket.

## Reference disks

```bash
metacomputer refdisk create NAME --from FOLDER [--dir PATH]
metacomputer refdisk list [--json]
metacomputer refdisk delete NAME [--yes]
```

`create` packs a copy of the folder into a hybrid UDF and ISO 9660 image. Every file
and folder is reached through descriptors from the checked folder down, each opened without
following links and checked to be the entry that was listed, so a file replaced by a link
while it's packed is left out rather than followed. The image
is made with `hdiutil makehybrid`, and the file is read-only. In the VM it mounts as UDF:
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

Each entry is `name[:mountpoint][:rw]`; `:rw` applies to folder volumes (disk volumes
are always read-write, reference disks always read-only). They're attached at the next
`start` (or `restart`); a VM that's already running keeps what it started with.

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
- A folder path can't contain `:` (the VM runner splits on it), and a folder on a
  network filesystem gets the same warning about locks.

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

A folder volume, or a folder packed into a reference disk, is refused when it is,
holds, or is inside one of these. The folder is opened first (so a link to one is refused
too) and judged by the spelling the kernel gives the opened folder and by its identity
(device and inode), compared the way APFS compares names: so `~/library`, `/USERS/you/.SSH`
or `/System/Volumes/Data/Users/you/Documents` are the folders they name:
- your home folder itself, and Documents, Desktop, Downloads, Pictures, Movies, Music
  and Public themselves (a folder inside Documents is allowed), or any parent of them;
- anything in `~/Library` (iCloud Drive is `~/Library/Mobile Documents`, other cloud
  drives `~/Library/CloudStorage`), in a dot-folder of your home (`.ssh`, `.aws`,
  `.config`, ...) or in `SSH_DIR`;
- this metacomputer checkout, Lume's VMs (`~/.lume`), `STORAGE_DIR` itself and the
  storage registry;
- the folder holding any disk volume's or reference disk's image (a VM writing there
  could swap an image or a checkpoint snapshot for a link), and so any parent of it;
- an S3 volume's staging folder (`.NAME.mcstage.noindex`, beside its local copy), and so
  the folder holding both. Cleanup there removes only what metacomputer made: temporary names
  carry the sync's own session prefix, recorded before any is used, and only those, after an
  interrupted sync, are removed; nothing else in the staging folder or the local copy is ever
  deleted, whatever its name. A staging folder that isn't the one made is refused, untouched;
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
| Show it in Finder (`cua volume mount`, an NFS mount at `~/Cua Volume`) | A folder volume: a Mac folder you pick, shared into the VM (`volume create NAME --folder PATH`) |
| Read-write volume mounts (the default before Secure Spaces) | Folder volumes are read-only unless `:rw`; read-write ones are quarantined and watched |
| `.DS_Store` and `._*` stay on the Mac | `put` sends no Mac metadata; in a folder volume, both sides see the same files; S3 volumes don't sync `.DS_Store` or `._*` |
| Storage backend `s3`: your own bucket, `cua volume config set --backend s3 --endpoint ... --bucket ...` | An S3 volume: `volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P]` |
| `cua volume config set-keys` (keys from stdin to the credential store) | `volume keys NAME` (macOS's own prompt into the login Keychain) |
| Settings > Storage > Test connection: endpoint, keys, versioning | `volume test NAME` |
| Sync across computers through the bucket; a conflict keeps both, the other as `name (conflict from ...).ext` | The same through `rclone bisync` at start, on an interval and at stop; the other copy as `name.conflict1.ext` |
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
- **Cross-computer sync without S3:** two Macs share files only through a bucket (an
  S3 volume) or a network drive you mount yourself; there's no sync service of our own.
- **Seconds-fast sync and streaming:** an S3 volume syncs on an interval (60 s by
  default) and keeps a full local copy; it doesn't stream large files on demand or
  notice another computer's change within seconds.

## How it works

- `lib/storage.py` keeps the registry and the locks, checks the plan for a start, and
  writes the extra `lume run` arguments (`--disk` for each volume, in order; `--mount`
  for the first reference disk, `--usb-storage` for the rest).
- In the VM, `/opt/metacomputer/bin/storage-helper` (root-owned, run only through
  `sudo metacomputer-admin storage attach`) reads the plan on stdin. It finds each
  volume among the extra read-write virtio disks by its place in that order and checks
  its size against the image's; each reference disk by its volume label. Folders arrive
  as one virtiofs device (tag `com.apple.virtio-fs.automount`) holding one subfolder
  per shared folder, named after its last path component (`name (2)` for a repeat);
  the helper mounts it at `/run/metacomputer/vfs` (root only), checks that each
  read-only one really is read-only, and bind-mounts each at its mount point,
  `nosuid,nodev`, read-only unless it's `rw`. It mounts
  through descriptors opened without following links, undoes everything if one item
  fails, and records what it mounted in `/run/metacomputer/storage.json`, which
  `guest-status` reports.
- For a read-write folder, `start` records what's in it before the VM boots
  (`RUN_DIR/storage-snap-NAME.json`, through descriptors), and the watcher starts from
  that, with the VM's process. Every folder attachment takes a record (read-write ones
  exclusive, read-only ones shared), so later starts can refuse the overlaps above.
- `checkpoint --with-volumes`, `restore`, `checkpoint --delete`, `volume resize` and
  `volume delete` hold each disk volume they work on (and a checkpoint its own lock) through
  the same locks, from before they check it until every change is made, registry entries
  included, so no VM can attach it in between (and they're refused while a VM holds it). An
  S3 sync holds its shared copy the same way. The rules between records are symmetric and
  apply within one start too, and reference disks take a shared record as well.
- What the VM's helper answers is read with a size cap (64 KiB) and a deadline, and so is
  everything `put` reads back; a VM that floods or stalls is cut off.
- After `lume run` starts, every attached path must still lead to the file or folder that
  was checked (same device and inode), or `start` stops the VM again.
- In bounded mode the manifest is parsed as YAML (the subset manifests use; anything else,
  such as anchors, tags or duplicate keys, grants nothing), and permissions come only from
  `resources.files.read` and `.write`.
- The lock files are in `~/.local/share/metacomputer/storage/locks/`; each names the VM,
  its checkout and its process (with the process's start time, so a reused pid doesn't
  count; one whose start time can't be read isn't written: that `start` refuses), keyed by the item's canonical spelling, so two spellings of one image are one lock.
  `start` takes them before the VM boots, and `stop` (also `reset`, `restore`
  and `baseline`) lets go of them after it's off.
