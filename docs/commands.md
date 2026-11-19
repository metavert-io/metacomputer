# Commands and settings

Every `metacomputer` command and every setting in `config/local.env`, and how the
Claude Code plugin is set up. The [README](../README.md#everyday-commands) has a
short overview, and the [guide](guide.md) shows what each is for, task by task.

- [Commands](#commands)
- [Configuration](#configuration)
- [The Claude Code plugin](#the-claude-code-plugin)

## Commands

`metacomputer` (installed at `~/.local/bin/metacomputer`; `mcomp` is a short
alias). `metacomputer help` prints a summary.

| Command | What it does |
| --- | --- |
| `setup [--no-chrome] [--no-build] [--yes]` | Finish or repair the setup (`./install.sh`; finished steps are skipped). From the Claude Code plugin with no checkout yet, the one-line install shipped in the plugin, which says what it will do and asks first ([install.md](install.md#from-claude-code-first-the-plugin)) |
| `start [--no-volumes]` / `stop [--force]` / `restart` | Boot (about 12 s), attaching `VOLUMES` and `REFDISKS` (none with `--no-volumes`); shut down from inside the VM, forced after 90 s |
| `status [--json]` | Every layer: VM, gateway, SSH, desktop, accessibility, driver, live view, host sharing, the vault's accounts, events, MCP handshake. Exit 0 = ready |
| `who` / `release` | Who is using the VM; hand it over (one controller at a time) |
| `reset [--yes]` | Restore the clean baseline (asks first; evidence on the Mac is kept) |
| `checkpoint NAME [--replace] [--with-volumes]` / `checkpoint --delete NAME [--yes]` / `checkpoints` / `restore NAME [--yes]` | Saved states of the stopped VM, with its disk volumes if you ask (deleting and restoring ask first) |
| `volume create NAME --size 20G [--dir PATH]` / `volume create NAME --folder PATH` / `volume list [--json]` / `volume resize NAME --size 40G` / `volume delete NAME [--yes]` | Disk volumes (kept outside the VM, read-write to one VM) and folder volumes (a Mac folder, read-only unless `:rw`), attached with `VOLUMES` |
| `volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P]` / `volume keys NAME` / `volume test NAME` / `volume sync NAME [--force] [--resync]` | S3 volumes: a folder kept in step with a bucket; the keys (your own terminal, into the Keychain); a connection check; a sync now |
| `refdisk create NAME --from FOLDER [--dir PATH]` / `refdisk list [--json]` / `refdisk delete NAME [--yes]` | Read-only reference disks packed from a Mac folder, attached with `REFDISKS` |
| `baseline [--yes]` | Make the current VM the new baseline (the old one is kept as `-prev`) |
| `build [--replace]` | Build the VM and its baseline from scratch |
| `sh -c CMD \| --file SCRIPT \| - [--cwd DIR] [--timeout S] [--max-output B] [--read-only] [--json] [--raw] [-- ARGS]` | Run bash in the VM as the desktop user, with the desktop's environment |
| `inspect [SECTION...] [--minutes N] [--top N] [--json]` | Read-only snapshot of the VM: `system`, `windows`, `processes`, `ports`, `files`, `browser` |
| `task start SLUG` / `task finish NAME` | One folder per task: video, contact sheet, screenshots, files, `artifacts.json` |
| `put HOST_PATH... [--to GUEST_DIR] [--overwrite]` | Copy files into the VM (default `~/Inbox`), each checked by SHA-256; a taken name lands as `name (1).ext` unless `--overwrite` |
| `get GUEST_PATH [HOST_DIR] [--task NAME]` | Copy files out of the VM |
| `shot [HOST_PATH] [--task NAME]` | A PNG of the whole VM screen |
| `rename GUEST_PATH NEW_NAME` | Rename a file in the VM (a download saved under its id) |
| `record start [NAME] [FPS]` / `record stop` / `record status` / `record moments FILE` | Record the whole desktop; its key moments when the action log is on |
| `recordings [--sync] [N]` | List recent recordings; copy the VM's task runs to the Mac |
| `contact-sheet MP4 [OUT.png]` | 12 frames of a recording in one image |
| `web compare\|responsive\|a11y\|open-untrusted URL [...]` | Browser checks that need no model |
| `browser [default firefox\|chrome] [mode human\|page\|hybrid] [profiles [--delete NAME]]` | The skill's defaults; named Chrome profiles |
| `view [--open]` | Watch or take over the desktop (VNC on loopback) |
| `input-reset [--check]` | Remove the driver's leftover virtual input devices (when Chrome ignores typing) |
| `desktop-restart` | Log the desktop out and in again (closes every app) |
| `security [apply] [--json]` | The optional hardening and the shell channel, as configured and as the VM has them |
| `actions list\|show\|verify` | The signed action log (`ACTION_LOG=on`) |
| `secrets init\|set\|delete\|policy\|bind\|unlock` | The credentials vault: commands you run in your own terminal (they ask for the vault passphrase or a value); `set` on an existing name needs `--replace`; `policy NAME --approval session\|each` changes whether a secret asks (`--answer-timeout 30m` an api secret's answer time limit); `unlock [--for 30m] [--confirm each] [--touch-id] [--require-presence\|--no-require-presence]` |
| `secrets import-passwords --browser chrome [--profile N] \| --csv FILE [--site H] [--dry-run]`, `secrets import-session --browser chrome --site H --name N --to-profile P` | Bring logins in from Chrome or a Chrome, Passwords app, 1Password or Bitwarden export, or one site's cookies from your Chrome (your own terminal; macOS asks about Chrome's key) |
| `secrets rekey\|recover\|rotate-session-key` | New keys, or opening the vault with its recovery key (your own terminal) |
| `secrets audit-sink add\|remove\|show NAME --key` | Off-Mac copies of the audit log, and a sink's signing key (your own terminal) |
| `secrets approve ID [--passphrase] [--scope session\|unlock]`, `secrets rule add ... --for D`, `secrets presence on\|off\|auto` | Approve a waiting use, make an unattended rule, or set presence mode: you, from any terminal, with Touch ID, your login password or the passphrase |
| `secrets requests`, `secrets deny ID`, `secrets rule list\|remove ID`, `secrets lock`, `secrets settle` | What waits for you; refuse one; the rules; lock now; wait until the vault has finished what it was sent |
| `secrets list\|fill\|login\|session save\|session load` | What Claude uses while the vault is unlocked (`login NAME --password-selector CSS [--username-selector CSS]` fills both fields under one approval); values never come back |
| `secrets token NAME [--ttl 15m]`, `secrets token renew ID\|--all [--ttl 15m]`, `secrets tokens`, `secrets revoke ID\|--all` | A short-lived API token and the proxy's URL in the VM; keep live ones going (the same token in the VM); the live tokens; end them |
| `secrets status\|audit\|selftest`, `secrets audit --verify\|--head`, `secrets audit-sink list\|test NAME` | State, the audit history (no values) and its checks, the vault's self-test; the sinks and their backlog |
| `notify "TEXT" [--why login\|captcha\|2fa\|dialog\|approval\|other] [--action TEXT]` | Tell your event sinks that Claude is blocked on you |
| `events sink add\|remove NAME ...` | Add or remove an event sink (your own terminal) |
| `events sink list\|show\|test`, `events status\|tail`, `events emit`, `events drain`, `events watcher start\|stop\|status` | The sinks, the stream's state and local log, an event from a script, a retry now, the watcher |
| `learn report [--since 7d] [--project PATH] [--json] [--draft-with-claude]` | Mine your sessions with the VM for repeated mistakes and time sinks; propose lessons with their evidence |
| `learn review`, `learn approve\|reject\|remove ID...`, `learn list [--instructions]` | Go through the proposals; keep or drop lessons; see what every new session gets |
| `learn edit ID`, `learn add`, `learn reseal` | Change a lesson's text, write your own, or accept `lessons.json` after it changed outside `learn` (your own terminal) |
| `learn bench --config TEST_ENV [--yes]` | The benchmark with and without your lessons, on a test VM (asks first: it spends your Claude usage) |
| `test [A-F]` | The GUI smoke suite, each test recorded |
| `ssh [ops\|admin] [CMD...]` | A raw SSH shell in the VM (`ops` is the desktop user); prefer `sh` |
| `logs [N]` | The last lines of the Lume, gateway and VM logs |
| `where` | Project, config, skill, docs and results folders; VM names |
| `mcp-lazy` / `mcp` | The MCP bridge Claude Code uses, through the hub (`mcp` with `MCOMP_DIRECT_BRIDGE=1` is the hub's own connection; only the transport tests open it otherwise) |
| `call [--session NAME] TOOL 'JSON'` / `hub [status\|stop]` | One tool call through the hub (with `--session`, tokens and driver state carry over between calls); the hub's pid, clients and idle time |

`stop`, `reset`, `restore`, `baseline`, `test`, `desktop-restart` and `sh`
refuse while another session is using the VM; `--override` proceeds anyway.

## Configuration

`config/defaults.env` holds the pinned versions and defaults. `install.sh`
writes this Mac's settings to `config/local.env`, which is never committed and
overrides any default. [config/local.env.example](../config/local.env.example)
documents the common keys. `MCOMP_CONFIG=/path/to/file` uses another file
instead.

**The VM** (all but the names take effect on the next build):

| Key | Default | |
| --- | --- | --- |
| `VM_NAME`, `BASELINE_NAME` | `metacomputer`, `metacomputer-baseline` | Lume VM names |
| `VM_CPU`, `VM_MEMORY` | `4`, `8GB` | VM size |
| `BROWSERS` | `firefox chrome` | Browsers built in; `firefox` alone leaves Chrome out |
| `EXTRA_PACKAGES` | (none) | More Ubuntu packages, e.g. `inkscape gimp` |
| `VIRTUAL_AUDIO` | `off` | `on`: a sound card inside the VM that plays into nothing |
| `SECRETS_PROXY_PORT` | `18765` | The port inside the VM (on its loopback) where the secrets API proxy appears |

**Lume and host sharing:**

| Key | Default | |
| --- | --- | --- |
| `LUME` | `~/.local/bin/mcomp-vm` | The Lume that runs the VM: mcomp-vm, or `~/.local/bin/lume` for Cua's release (`install.sh --lume=standard`). If it isn't installed and `~/.local/bin/lume-secure`, mcomp-vm's former name, is, metacomputer uses that and says so |
| `SHARE_CLIPBOARD`, `SHARE_AUDIO`, `SHARE_ROSETTA` | `off` | `on` connects the VM to your Mac's clipboard, audio or Rosetta (mcomp-vm only; standard Lume always shares) |

**Mac side:**

| Key | Default | |
| --- | --- | --- |
| `SSH_DIR` | `~/.ssh/metacomputer` | SSH keys, the pinned host key and the live-view password |
| `SSH_PORT`, `VNC_PORT` | `2277`, `5977` | Loopback ports for the VM's SSH and live view |
| `RUN_DIR`, `LOG_DIR` | `.run/`, `logs/` in the checkout | Runtime files and logs; a second VM needs its own |
| `VNC_PASSWORD_STORE` | `file` (a new install writes `keychain`) | `keychain` (the login Keychain, item `metacomputer-vnc`), `file` (0600 in `SSH_DIR`), or `command` with `VNC_PASSWORD_GET` / `VNC_PASSWORD_SET` for your own secret store |
| `IMPORT_MIN_FREE_GB` | `10` | Copies from the VM, and read-write folder and S3 volumes, stop before the Mac's free space falls below this |

**Skill defaults** (`metacomputer browser` changes them):

| Key | Default | |
| --- | --- | --- |
| `BROWSER` | `firefox` | `firefox` or `chrome` |
| `MODE` | `human` | `human`, `page` or `hybrid` |

**The hub** (one connection per VM, shared by every client; [architecture.md](../architecture.md#the-hub)):

| Key | Default | |
| --- | --- | --- |
| `MCP_HUB` | `on` | `on`: Claude Code's bridge, `metacomputer call` and scripts share one hub per VM, which applies every guard to each of them; `off`: each connects on its own, with the same guards |
| `HUB_IDLE` | `10m` | The hub exits after this long with no client and no driver recording |
| `COORD_GUARD` | `on` | `on`: pixel input is refused when certain window-layout changes happened since the screenshot it was aimed from (a window resized, or covered by a dialog or another app; it compares geometry, not content); `off`: only Cua Driver's own checks |

**Lessons** (`metacomputer learn`; [docs/learn.md](learn.md)):

| Key | Default | |
| --- | --- | --- |
| `LESSONS` | `on` | `on`: the lessons you approved go in every new session's MCP instructions, as advice; `off`: kept, not sent |
| `LESSONS_DIR` | `~/.local/share/metacomputer/lessons` | Their private store (a 0700 folder, 0600 files, read only when private) |

**Shell channel** (`metacomputer sh`; [docs/shell.md](shell.md)):

| Key | Default | |
| --- | --- | --- |
| `VM_SHELL` | `auto` | `auto`: on, except while `DRIVER_MODE=bounded`; `on`: always; `off`: every run is refused |
| `SHELL_LOG` | `auto` | `auto`: each run goes in the signed action log when `ACTION_LOG=on`; `off`: never logged |

**Storage** ([docs/storage.md](storage.md); each takes effect at the next start):

| Key | Default | |
| --- | --- | --- |
| `STORAGE_DIR` | `~/.local/share/metacomputer` | Where new volumes and reference disks go: any folder you can reach, such as an external drive or a NAS mount (`--dir` per item) |
| `VOLUMES` | (none) | Volumes to attach: `name[:mountpoint][:rw]`, comma-separated; default mount point `~/data/NAME`. Disk volumes are read-write; folder volumes read-only unless `:rw` |
| `REFDISKS` | (none) | Read-only reference disks to attach: `name[:mountpoint]`; default `~/ref/NAME` |
| `S3_SYNC_INTERVAL` | `60` | Seconds between syncs of S3 volumes while the VM runs (they also sync at start and stop) |
| `S3_BACKUP_MAX_GB` | `5` | How much of the local files a sync overwrote or deleted is kept (newest first) |

**Desktop recordings** (`metacomputer record`):

| Key | Default | |
| --- | --- | --- |
| `RECORD_FPS` | `20` | Frames per second |
| `RECORD_QUALITY` | `standard` | `high`: sharp enough to edit into videos, several times larger |
| `RECORD_TO` | `guest` | `host`: stream to the Mac while recording, so nothing builds up in the VM |
| `RECORD_DIR` | `recordings/manual/` | Any folder on the Mac, such as an external drive; only its last part is created |
| `RECORD_AUDIO` | `auto` | `auto`: include the VM's sound when it has the virtual sound card; `off`: video only |

**Optional hardening** ([SECURITY.md](../SECURITY.md)):

| Key | Default | |
| --- | --- | --- |
| `ACTION_LOG` | `off` | `on`: signed log of every tool call and shell run, in `logs/actions/` |
| `ACTION_LOG_TEXT` | `redact` | `keep`: record typed text, values, scripts and shell output in full |
| `ACTION_LOG_KEY` | `$SSH_DIR/action-log` | The signing key, or a `.pub` whose private key is in ssh-agent (for example hardware-backed) |
| `SSH_AUTH` | `key` | `cert`: a fresh key and a short-lived certificate per connection |
| `SSH_CERT_TTL`, `SSH_CA_KEY` | `5m`, `$SSH_DIR/ca` | Certificate lifetime; the CA key, or a `.pub` in ssh-agent |
| `DRIVER_MODE` | `standard` | `bounded`: Cua Driver does only what a manifest lists |
| `DRIVER_PROFILE` | `desktop` | `readonly`, `desktop` or `browser` ([config/driver-profiles/](../config/driver-profiles/)) |
| `DRIVER_ORIGINS` | (none) | The sites the `browser` profile may open, e.g. `"https://example.com https://app.example.com"` |
| `DRIVER_MANIFEST` | (none) | Your own Cua capability manifest instead of a profile |
| `DRIVER_EXPIRES`, `DRIVER_IDLE` | `8h`, `30m` | A bounded session's lifetime (at most 24h) and idle limit |

**The event stream and the vault** keep their settings elsewhere: event sinks
in `~/.config/metacomputer/events.json` (changed only with `metacomputer events
sink`; `MCOMP_EVENTS_DIR` moves the folder, `MCOMP_EVENTS_SPOOL` the drop folder
for other programs' events), and the vault in
`~/Library/Application Support/metacomputer/` (`MCOMP_VAULT_DIR`).

## The Claude Code plugin

`./install.sh` installs the plugin `metacomputer@metacomputer` at user scope, from
the marketplace `metacomputer` (your checkout), so the skills and tools are there
in every project. It brings:
- the `metacomputer` skill ([skills/metacomputer/](../skills/metacomputer/)): when a
  task belongs on the virtual computer, and how to drive this desktop;
- Cua Driver's own guide, as `metacomputer:cua-driver`;
- `/metacomputer:learn`, which runs the lessons report in a chat;
- the MCP server `metacomputer` (`…/bin/metacomputer mcp-lazy`), which lists the
  tools even while the VM is off and reaches the VM through the hub.

**Tool names.** `mcp__plugin_metacomputer_metacomputer__*` from the plugin, and
`mcp__metacomputer__*` without it (`./install.sh --no-plugin`: a skill link at
`~/.claude/skills/metacomputer` and a user-scope MCP server). The skill works with
either. Permission rules, scripts and MCP configurations of your own that name
`mcp__metacomputer__` need the plugin's prefix too, for example
`"permissions": {"allow": ["mcp__plugin_metacomputer_metacomputer__*"]}`. An earlier
install's skill link and user-scope server are removed when they point at the same
checkout, so the tools aren't listed twice.

**Updates.** Installed from the checkout, the plugin loads in place: after a
`git pull`, new sessions pick up the new skills. Installed from GitHub
(`/plugin marketplace add metavert-io/metacomputer`, then
`/plugin install metacomputer@metacomputer`; on Claude Code 2.1.275 or later,
`/plugin install metacomputer --marketplace metavert-io/metacomputer` does both),
Claude Code keeps a copy that follows the repository's commits: update it with
`claude plugin update metacomputer@metacomputer`. That copy's `bin/metacomputer`
runs your checkout's, through `~/.local/bin/metacomputer` or the checkout
`MCOMP_HOME` names; with no checkout yet, it can only run `setup`
([install.md](install.md#from-claude-code-first-the-plugin)).

**Whose plugin.** A marketplace named `metacomputer` that's already registered is
used only when it's this checkout or github.com/metavert-io/metacomputer. From
anywhere else, its plugin isn't installed, since it would run that source's code
on your Mac; the installer says how to remove it and sets up the skill link and
server instead.
