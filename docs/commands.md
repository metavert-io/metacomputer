# Commands and settings

Every `metacomputer` command and every setting in `config/local.env`, and how the
Claude Code plugin is set up. The [README](../README.md#everyday-commands) has a
short overview, and the [guide](guide.md) shows what each is for, task by task.

- [Commands](#commands)
- [Configuration](#configuration)
- [The Claude Code plugin](#the-claude-code-plugin)

## Commands

`metacomputer` (installed at `~/.local/bin/metacomputer`; `mcomp` is a short
alias). `metacomputer help` prints a summary. `metacomputer --vm NAME COMMAND`
runs any command on the VM `NAME`; without `--vm`, a command acts on your default
computer ([More than one VM](#more-than-one-vm)).

| Command | What it does |
| --- | --- |
| `--vm NAME COMMAND` | Any command, on the VM `NAME` (its settings: `config/vms/NAME.env`, or `config/local.env` when that's the VM it names). `MCOMP_VM=NAME` does the same |
| `--on PLACE COMMAND` | Any command, on the computer at `PLACE`: `mac` (this Mac: your default computer when it's there, else the one computer there), `server` (your own server), `oci` (Oracle Cloud) or `cloud` (anywhere but this Mac), matched against places only (a VM named like a place is never what `--on` means; `vm create` refuses those names and `list` flags one made by hand). A place with no computer there says so and runs nothing (`--on cloud` with every computer on this Mac: this metacomputer has no cloud computers set up). With `--vm` too, both must agree (`--vm demo --on cloud` is refused for a VM on this Mac). A computer's name goes with `--vm`, not `--on` (`lib/placement.py`) |
| `list [--on PLACE] [--json]` | Every computer: the same as `vm list`. `--on` keeps those at `PLACE` (`--on cloud`: none, saying so) |
| `default [NAME] [--json]` | Your default computer, the one a command acts on when no VM is named, and where its settings are. With `NAME`: makes `NAME` the default after checking it resolves to settings exactly as `--vm NAME` would (`config/vms/NAME.env`, or the main VM's name, which removes `config/default-vm`: `config/local.env`'s VM again). It writes `config/default-vm` in one step (a temporary file renamed over it) and changes nothing else: no VM is started, stopped or changed, and a hub or session already running keeps the VM it started with. It holds `NAME`'s lock (the one a build or `vm delete` of it holds) from the check to the write, so it never makes a VM being deleted the default. `--vm` and `--on` are refused with it |
| `vm list [--on PLACE] [--json]` | This checkout's VMs (from its settings files and the runner): where each is (`WHERE`: this Mac), state, disk use, baseline (and checkpoints), ports, settings file; the runner's other VMs; problems such as a port two VMs share, or a default that doesn't resolve. `*` marks the VM these settings act on: without `--vm`, your default computer. `--json` adds `place`, `where` and `default` to each VM, and `default` (the stored default's name, whatever chose this command's VM) and `overridden_by` (what chose another: `--vm`, `MCOMP_VM`, `MCOMP_CONFIG` or `MCOMP_NO_DEFAULT_VM`) to the listing |
| `vm create NAME --on server --host ADDRESS --user ACCOUNT [--port P] [--egress internet\|off] [--no-build] [--yes]` | Your own Ubuntu 24.04 server as a computer ([remote.md](remote.md)): writes `config/vms/NAME.env` (`PLACE=server`, its address, the account that sets it up), then sets it up once over your own SSH with sudo (asks first; `--yes` doesn't). metacomputer never powers it on or off |
| `vm create NAME --on oci [--size arm-2x12\|arm-1x6\|x86-1x8\|x86-2x8\|x86-micro] [--egress internet\|off] [--idle-stop MIN\|off] [--no-build] [--yes]` | A computer Oracle Cloud makes ([remote.md](remote.md#oracle-cloud-oci)), with `OCI_*` credentials in the command's environment: says the rate and what's kept, then makes the instance (and the network, the first time), trying each availability domain once when one is full, and sets it up |
| `cloud status\|doctor\|setup\|teardown oci [--json]` | What metacomputer made in Oracle Cloud (instances, network, boot volumes, tagged), checks (credentials, the network lets in SSH only), make the network, delete it once no instance is left. Credentials from the environment |
| `vm create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes]` | A VM of its own: writes `config/vms/NAME.env` (its names, ports, SSH keys, vault folder and spool, plus this Mac's choices such as `LUME`, `BROWSERS` and the hardening settings) and builds it after asking (`--yes` doesn't ask; `--no-build` only writes the file). Without ports, the first free pair from 2300 (live view = SSH + 3700) |
| `vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes]` | Delete the VM, its baseline (and `-prev`) and `config/vms/NAME.env`, after saying what goes and what stays, once you type its name (no terminal: `--confirm NAME`; `--yes` never counts). Refuses the VM this checkout uses without `--vm` (the main VM, and your default computer when `config/default-vm` names another), a running VM and one a session holds, unless `--force` (and, as a VM that may be the default, any VM while `config/default-vm` can't be read); refuses while it has checkpoints, when anything it would delete is another VM's VM, baseline or copy, and, every time (with or without flags, the main VM too), unless every other settings file reads cleanly and the identity rule holds (below: no folder in another VM's or in the runner's VM storage, where the runner's delete reaches). Holds the VM's lock throughout and its owners lock once you confirm, checks everything again then, and only then stops the VM's watcher and hub. A settings file that's refused still protects the names, folders and volumes it says; one whose folders or volumes can't be known stops any recursive deletion and `--delete-volumes`. Deletes folders only along a path with no links, through `lib/safedelete.py` like every other delete: never one that is or holds another VM's folder, the account's own vault or spools, the home folder or the checkout. Keeps volumes and the vault folder unless `--delete-volumes` (each volume asks again) or `--delete-vault` (only the VM's own folder). The typed name guards against a slip; it isn't proof a person typed it (any program with a terminal can) |
| `setup [--no-chrome] [--no-build] [--yes]` | Finish or repair the setup (`./install.sh`; finished steps are skipped). From the Claude Code plugin with no checkout yet, the one-line install shipped in the plugin, which says what it will do and asks first ([install.md](install.md#from-claude-code-first-the-plugin)) |
| `start [--no-volumes]` / `stop [--force]` / `restart` | Boot (about 12 s), attaching `VOLUMES` and `REFDISKS` (none with `--no-volumes`); shut down from inside the VM, forced after 90 s. `start` takes the VM's lock, so it never runs during a build, reset, restore or `vm delete` of it. A computer elsewhere: `start` starts a provider's stopped instance (credentials in the environment) and waits for SSH and the desktop; `stop` shuts it down and the provider stops it (your own server: only this Mac's connection closes) |
| `status [--json]` | Every layer: VM, gateway, SSH, desktop, accessibility, driver, live view, host sharing, the vault's accounts, events, MCP handshake. Exit 0 = ready |
| `who` / `release` | Who is using the VM; hand it over (one controller at a time) |
| `reset [--yes] [--discard-unsynced]` | Restore the clean baseline (asks first; evidence on the Mac is kept). It copies the VM's recordings first and stops, deleting nothing, if that copy isn't whole; `--discard-unsynced` goes on without what wasn't copied (`--yes` doesn't). Like `restore`, `checkpoint` and `baseline`, it checks the identity rule (below) after taking the VM's lock and refuses before anything is replaced |
| `checkpoint NAME [--replace] [--with-volumes]` / `checkpoint --delete NAME [--yes]` / `checkpoints` / `restore NAME [--yes] [--discard-unsynced]` | Saved states of the stopped VM, with its disk volumes if you ask (deleting and restoring ask first; `restore` copies the recordings first, as `reset` does) |
| `volume create NAME --size 20G [--dir PATH]` / `volume create NAME --folder PATH` / `volume list [--json]` / `volume resize NAME --size 40G` / `volume delete NAME [--yes]` | Disk volumes (kept outside the VM, read-write to one VM) and folder volumes (a Mac folder, read-only unless `:rw`), attached with `VOLUMES` |
| `volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P]` / `volume keys NAME` / `volume test NAME` / `volume sync NAME [--force] [--resync]` | S3 volumes: a folder kept in step with a bucket; the keys (your own terminal, into the Keychain); a connection check; a sync now |
| `refdisk create NAME --from FOLDER [--dir PATH]` / `refdisk list [--json]` / `refdisk delete NAME [--yes]` | Read-only reference disks packed from a Mac folder, attached with `REFDISKS` |
| `baseline [--yes] [--discard-unsynced]` | Make the current VM the new baseline (the old one is kept as `-prev`). The VM's recordings are copied to the Mac and then removed from it, only once the copy is whole, as for `reset` |
| `build` (a computer elsewhere) | Set it up (`scripts/remote-build.sh`): a provider's instance is made if there's none; then the payload over SSH, `provision.sh`, the reboot, the checks and `finalize.sh`. Run again, it changes nothing that's already right |
| `build [--replace]` | Build the VM and its baseline from scratch. Refuses, before anything is deleted, unless every other settings file reads cleanly and the identity rule holds for this VM (below); checked again before and after the downloads |
| `sh -c CMD \| --file SCRIPT \| - [--cwd DIR] [--timeout S] [--max-output B] [--read-only] [--json] [--raw] [-- ARGS]` | Run bash in the VM as the desktop user, with the desktop's environment |
| `claude [start] [--token NAME] [--ttl 1h] [--model M] [--skip-permissions] [--session NAME] [--cwd DIR] [--show] [--no-install] [-- CLAUDE_ARGS]` | Claude Code inside the VM, in tmux: installed if missing, signed in through the vault's proxy with a short-lived token for an api secret (`Authorization: Bearer` → `CLAUDE_CODE_OAUTH_TOKEN`, `x-api-key` → `ANTHROPIC_API_KEY`), first-run choices set ([guide](guide.md#claude-inside-the-metacomputer)) |
| `claude send TEXT\|--file F\|- [--no-enter] [--wait] [--quiet S] [--timeout S]` / `claude read [--lines N\|--all]` / `claude wait` / `claude keys KEY...` / `claude show [--again]` / `claude status` / `claude renew [--ttl D]` / `claude stop [--revoke]` | Talk to it through tmux (a bracketed paste, then Enter), read its screen (escaped), wait for it to settle, answer its questions, put it on the VM's desktop, check it and its token, keep its token going (`secrets token renew`, asked like a new token), end it (and its token). Each takes `--session NAME`, `--json`, `--override` |
| `inspect [SECTION...] [--minutes N] [--top N] [--json]` | Read-only snapshot of the VM: `system`, `windows`, `processes`, `ports`, `files`, `browser` |
| `task start SLUG [--no-sync]` / `task finish NAME` | One folder per task: video, contact sheet, screenshots, files, `artifacts.json`; the recording's finished turns are copied to the Mac while it's open (`TASK_SYNC_INTERVAL`; `--no-sync` for a task that records nothing) |
| `put HOST_PATH... [--to GUEST_DIR] [--overwrite]` | Copy files into the VM (default `~/Inbox`), each checked by SHA-256; a taken name lands as `name (1).ext` unless `--overwrite` |
| `get GUEST_PATH [HOST_DIR] [--task NAME]` | Copy files out of the VM |
| `shot [HOST_PATH] [--task NAME]` | A PNG of the whole VM screen |
| `rename GUEST_PATH NEW_NAME` | Rename a file in the VM (a download saved under its id) |
| `record start [NAME] [FPS]` / `record stop` / `record status` / `record moments FILE` | Record the whole desktop, streamed to the Mac as it records; its key moments when the action log is on |
| `recordings [--sync] [N]` | List recent recordings; copy the VM's task runs to the Mac |
| `contact-sheet MP4 [OUT.png]` | 12 frames of a recording in one image |
| `web compare\|responsive\|a11y\|open-untrusted URL [...]` | Browser checks that need no model (`responsive --browser webkit`: GNOME Web, when the VM has it; `responsive --device iphone-15,pixel-8,...`: phone and tablet emulation) |
| `web gpu [URL] [--browser chrome\|firefox\|both]` | What WebGL and WebGPU a page gets in the VM (software: the VM has no GPU), whether the page asked for a GPU, its GPU-related errors, a screenshot |
| `web debug URL [--browser B] [--device D] [--keep-query]` | Console messages, uncaught errors, failed and slowest requests, timings, a screenshot |
| `web read URL [--max-chars N]` | The page's main text as Markdown |
| `web page open URL [--name N] \| read [--text] \| fill REF VALUE \| select REF OPTION \| click REF \| upload REF GUEST_PATH \| close` | Precise page tools in a Firefox of its own (WebDriver BiDi): controls by ref, exact values, uploads; refuses password and one-time-code fields |
| `trace start NAME [--session S] \| stop \| list \| show NAME \| delete NAME` | Record a session's browser steps through the hub (`RUN_DIR/traces/`, 0600; at most 24 h and 20 MB) |
| `trace export NAME [--url URL] [--out FILE]` | The trace as a Playwright test (TypeScript): role/name locators, fills, keys, assertions, navigations; pixel clicks marked; page text only in escaped string literals |
| `version [--json]` | This checkout's version, what's installed, and for each VM what built it and the gateway it runs |
| `update [--check] [--restart\|--no-restart] [--ref TAG] [--allow-downgrade]` | Update to the latest release and finish the install: services on older code restarted, a VM on an earlier gateway restarted (asks first, or `--restart`), what's left (a `secrets bind`, a rebuild that would add something). `--check`: only what's new |
| `each VM,VM,... -- COMMAND [ARGS]` | The same metacomputer command on several VMs at once (`main` is this checkout's VM), without a terminal; output per VM, exit status the highest |
| `net [status [--json]]` | What the VM can reach now: the open internet (with config/netgw-rules.json's refusals) or a network allowlist and when it ends |
| `net allow NAME... [--for D] [--exact]` | With no list in force: start one (anyone, Claude too; 2 h by default, 1 min to 24 h); the VM then reaches only those names (and their subdomains unless `--exact`), enforced by the gateway on this Mac. With one in force: add names or time, at your own terminal only. With the VM running it says the list is in force only when the gateway reports the rules just written (exit 4 otherwise) |
| `net remove NAME... [--exact]` | Take names off the list in force (anyone) |
| `net clear` | Lift the list before it ends (your own terminal only); it also ends by itself when its time is up |
| `browser [default firefox\|chrome] [mode human\|page\|hybrid] [pacing human\|fast] [profiles [--new NAME \| --delete NAME [--firefox]]]` | The skill's defaults and human pacing; kept Firefox profiles (`--new`: made in the VM, with the `launch_app` call that opens one) and named Chrome profiles |
| `view [--open \| --print \| --tailnet \| --tunnel \| --forget] [--remember \| --no-remember]` | Watch or take over the desktop (VNC on loopback). `--open` (and plain `view` in Terminal on the Mac's screen): Screen Sharing on this Mac. The first time, at a terminal, it offers to save the live-view password in your login keychain, where Screen Sharing finds it (`VIEW_REMEMBER`); once saved, Screen Sharing connects without asking, and each `--open` keeps it up to date. Otherwise the password goes on the clipboard for 45 s (`VIEW_CLIPBOARD_S`); `--no-remember` does that this once. Over SSH, or with no terminal, plain `view` prints how to connect, as `--print` does. `--remember`: save it now; `--forget`: remove it (both only in your own terminal; `--remember` refuses while `VIEW_REMEMBER=off`). `--tailnet`: the `tailscale serve` command that would publish it to your tailnet, and its undo (printed, never run). A computer elsewhere: `--open` first opens an SSH tunnel to its live view (x11vnc on its own `127.0.0.1:5900`) from `127.0.0.1:VNC_PORT` with its `view` key, which closes when the viewer does; `--tunnel` opens only the tunnel, for another VNC client; `--tailnet` is refused |
| `input-reset [--check]` | Remove the driver's leftover virtual input devices (when Chrome ignores typing, or the live view ignores your mouse or keyboard). Opening a handoff and `view --open` do it too. Not while an agent call is still running (exit 3); during a handoff only in your own terminal |
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
| `secrets fill NAME --field password\|username\|otp --human [--window-id N]` | The same into a human-mode browser (the VM's regular Firefox, or a Chrome started normally): its site from the address bar, the focused field checked, typed as real keys through the driver ([SECURITY.md](../SECURITY.md#human-mode-fills-the-address-bar-instead-of-devtools)) |
| `secrets ask --site S --kind code\|link --why TEXT [--into TAB/FIELD \| --human] [--ttl 10m] [--wait 5m] [--json]` | A one-time code or sign-in link request for you: prints its id at once, sends `needs_you.code_requested`, and with `INTERACTIVE_PROMPTS=on` a dialog asks for it on the Mac. `--wait` blocks until it's filled, refused or expired |
| `secrets give ID [--json]` | The value for a waiting request, from stdin only (hidden at a terminal, one line from a pipe; never the command line): a terminal follows the usual approval, a pipe needs `rule add code` or exits 7 |
| `secrets code --site S (--into TAB/FIELD \| --human) [--kind link] [--json] < value` | Ask, give and fill in one step from a script: it gets `{"status":"filled"}` or a refusal, never the value |
| `secrets relay --from TAB/ELEMENT --to TAB/FIELD --site S [--kind code\|link] [--pattern digits\|alnum] [--length N\|A-B] [--json]` | Read a code (or the site's sign-in link) from a page element in the VM's page-mode Chrome and fill (open) it: `relayed: filled` or why not |
| `secrets fill --request ID [--selector CSS \| --human]`, `secrets rule add code --site S --from stdin [--link-host H] [--project DIR] [--expires 7d]`, `secrets rule add relay --from-host H --to S [...]` | A given request into the tab on its site; your standing approvals for codes piped in with no terminal, or relayed (never an identity provider). Exit codes for these: 0 filled, 2 refused, 3 locked, 6 expired, 7 needs approval (`secrets approve ID`) |
| `secrets token NAME [--ttl 15m]`, `secrets token renew ID\|--all [--ttl 15m]`, `secrets tokens`, `secrets revoke ID\|--all` | A short-lived API token and the proxy's URL in the VM; keep live ones going (the same token in the VM); the live tokens; end them |
| `secrets status\|audit\|selftest`, `secrets audit --verify\|--head`, `secrets audit-sink list\|test NAME` | State, the audit history (no values) and its checks, the vault's self-test; the sinks and their backlog |
| `notify "TEXT" [--why login\|captcha\|2fa\|dialog\|approval\|other] [--action TEXT]` | Tell your event sinks that Claude is blocked on you |
| `handoff --why captcha\|login\|2fa\|other --say "TEXT" [--timeout 15m] [--json] [--override]` | Claude hands the computer to you and waits: it notifies you (and, with `INTERACTIVE_PROMPTS=on`, shows a dialog on the Mac's screen with an Open live view button), and every acting call waits until you're done. Exit 0 done, 4 cancelled, 124 time limit (at most 2 h), 5 stopped waiting (still open), 3 refused (one is open, another session is acting, or an action is still on its way, such as a fill waiting for approval) |
| `handoff done [--note TEXT]`, `handoff cancel [--note TEXT]` | You: hand the computer back (Claude carries on), or call the handoff off (Claude stops and asks). The session that asked can't say "done" for you without a terminal |
| `handoff wait [--json]`, `handoff status [--json]` | Wait again for the open handoff (or see how the last one ended); show it |
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
| `mcp-lazy` / `mcp` | The MCP bridge Claude Code and Codex use, through the hub (`mcp` with `MCOMP_DIRECT_BRIDGE=1` is the hub's own connection; only the transport tests open it otherwise). Besides the driver's tools it has two of its own, `list_computers` and `select_computer` ([An agent session's computer](#an-agent-sessions-computer)) |
| `call [--session NAME] TOOL 'JSON'` / `hub [status\|stop [--if-idle]]` | One tool call through the hub (with `--session`, tokens and driver state carry over between calls); the hub's pid, clients and idle time; `stop --if-idle` stops it only when no client is connected, no call or recording is running and no handoff is open (exit 3 otherwise) |

`stop`, `reset`, `restore`, `baseline`, `test`, `desktop-restart`, `sh` and
`claude` (but `claude read`, `wait` and `status`) refuse while another session is
using the VM; `--override` proceeds anyway.

`start`, `stop`, `restart`, `reset`, `restore`, `baseline`, `checkpoint`, `build`,
`desktop-restart`, `test`, `security apply`, `vm create` and `vm delete` first print
which VM they act on, and every `secrets` command and every `claude` subcommand
that acts (all but `read`, `wait` and `status`; its token is the vault's) which VM
and vault, in one line on stderr. What `claude` starts (its keeper, the keeper's
probes, its `secrets` calls) runs with the same VM's settings. For example: `metacomputer: VM demo (config/vms/demo.env), vault ~/Library/Application Support/metacomputer-vms/demo`.

## Configuration

`config/defaults.env` holds the pinned versions and defaults. `install.sh`
writes this Mac's settings to `config/local.env`, which is never committed and
overrides any default. [config/local.env.example](../config/local.env.example)
documents the common keys. `MCOMP_CONFIG=/path/to/file` uses another file
instead.

### More than one VM

Every command reads `config/defaults.env`, then **one** settings file, never two
layered:

| What picks the VM | Settings file |
| --- | --- |
| `--vm NAME` (before the command), or `MCOMP_VM=NAME`; `--vm` wins | `config/vms/NAME.env`; `config/local.env` when that is the VM it names; the defaults alone for `--vm metacomputer` in a checkout with no `config/local.env` |
| `MCOMP_CONFIG=FILE` | that file, as before (with `--vm` too, both must name the same file) |
| neither, with `config/default-vm` (your default computer: `metacomputer default NAME` writes it) | the VM it names, exactly as `--vm NAME` would choose it, so every refusal of `--vm` applies; passed on to what the command starts as its settings file |
| none of these | `config/local.env`, this checkout's main VM |

`config/default-vm` is never committed. It must be a plain file (not a link) holding one
VM name on one line and nothing else; anything else is refused, as is a name with no
settings: it never falls back to another VM. It's read once, by one reader for both sides
(`lib/defaultvm.py`: opened without following a link or waiting on a FIFO, checked to be a
regular file through that same descriptor, and exactly the bytes read are checked), so what
is checked is what is used. `metacomputer list`, `vm list`, `default` and
`help` still work then, and say what's wrong. `MCOMP_NO_DEFAULT_VM=1` leaves it out (a
command that names no VM acts on `config/local.env`'s VM); `setup`, `update`, `version`,
`each`'s `main` and `install.sh`'s build always mean `config/local.env`'s VM, whatever the
default is. Whatever a command chose is passed on to what it starts (a VM chosen by name or
as the default as its settings file; the main VM, chosen because there's no default, as
`MCOMP_NO_DEFAULT_VM=1`), so a default set meanwhile never moves a build, a hub or a script
it started to another VM. `lib/labconfig.py` makes the same choice for the Python side,
with the same checks: it reads the pointer with the same reader and resolves it with
`scripts/lib.sh`'s own `mc_load_settings`, in a bash that reads no startup file and imports
no function from the environment, which hands back the settings it loaded, as values, in
one strict record on a pipe of its own. Python uses those values as they are and never
reads the file again; anything else (a refusal, a record that isn't exactly that) refuses,
never the defaults. Only the step that writes the record opens that pipe (a named one, in a
private folder), so a helper the resolution leaves running never holds up the answer; the
bash runs in a session of its own, and one that takes too long or says too much is killed
with its whole process group (what it started, unless that left the group).

A file in `config/vms/` is one VM's own (`vm create` writes it; `config/vms/` is
never committed). Its `VM_NAME` is its file name, and it must set its own
`SSH_PORT` and `VNC_PORT`. Its baseline is always `NAME-baseline`. Its folders are
fixed by its name and can't be set in the file (one that sets `SSH_DIR`, `RUN_DIR`,
`LOG_DIR`, `VAULT_DIR` or `EVENTS_SPOOL` is refused, saying why): `SSH_DIR`
`~/.ssh/metacomputer-NAME`, `RUN_DIR` and `LOG_DIR` `.run-vms/NAME` and
`logs-vms/NAME`, and the vault folder and spool below. `config/local.env` isn't read
for it (`vm create` copies this Mac's choices into the file once). None of these is
inside a folder of the main VM's (`.run`, `logs`, the account's vault folder) or of
another VM's (below).

| Key | Default | |
| --- | --- | --- |
| `VAULT_DIR` | the vault's own folder, `~/Library/Application Support/metacomputer` (the account's home, whatever `$HOME` says); for a `config/vms/` VM, `.../metacomputer-vms/NAME` (beside it, not in it) | This VM's secrets vault, handed to `mcomp-vault` as `MCOMP_VAULT_DIR` |
| `EVENTS_SPOOL` | the event stream's own spool, `~/Library/Application Support/metacomputer/events/spool`; for a `config/vms/` VM, `.../metacomputer-vms/NAME/events/spool` | Where this VM's vault drops events, and where its event watcher takes them from |

`MCOMP_VAULT_DIR` and `MCOMP_EVENTS_SPOOL` in the environment still override these
for the main VM and `MCOMP_CONFIG` VMs (they must be absolute); one that isn't the same
folder as a file's own `VAULT_DIR` or `EVENTS_SPOOL` is refused. For a `config/vms/` VM
they may only name its own folders. Both reach the vault as the folders they are, with
links resolved.

Refused rather than guessed (nothing runs):
- `--vm NAME` with no settings for `NAME`, or with two (`config/local.env` and
  `config/vms/NAME.env`); `--vm` and an `MCOMP_CONFIG` naming different files;
- a `config/default-vm` that isn't one VM name on one line, or names a VM that has no
  settings here (or settings that are refused); `--on` with a place that has no computer,
  or more than one and none of them the default, and `--vm` and `--on` that disagree;
- a `config/vms/` file that names another VM, has no ports of its own, is named like a
  VM's copy (`-baseline`, `-baseline-prev`, `-ckpt-`), sets another `BASELINE_NAME`, or
  shares a name, a port or a folder with the main VM or any other `config/vms/` VM;
- the identity rule: no VM's folder (`SSH_DIR`, `RUN_DIR`, `LOG_DIR`, `VAULT_DIR`,
  `EVENTS_SPOOL`) may be another VM's, be inside one or hold one, and none may be in,
  or hold, the runner's VM storage (`~/.lume`, and every storage location the runner or
  Lume's settings list). Every other recursive delete on the Mac goes through
  `lib/safedelete.py` (`scripts/safe-rmtree.sh`), which resolves a target once, refuses
  one that, as it physically is, is or holds any VM's folder (or a link on the way to
  one), the account's own vault and spools included, and deletes exactly what it checked
  (a path ending in `/`, `/.` or `/..` is refused); a cleanup that walks a folder (S3
  backups, old SSH certificates) checks that folder first and follows no link. A
  `config/vms/` VM is refused when it's read if the rule fails. `build`, `vm delete`, `reset`, `restore`, `checkpoint` (deleting or replacing
  one) and `baseline` check it after taking the VM's lock, for whichever VM they change
  (the main VM and `MCOMP_CONFIG` VMs included), before anything is deleted, replaced or
  rotated, and name the file and folder at fault. Folders are compared as the folders
  they physically are (every link at every level followed; device and inode; names that
  don't exist yet without case), so a link anywhere, or another spelling, can't put one
  inside another. When the runner's storage can't be known (no runner, `config get`
  failing or listing none, a settings file of Lume's or a location that can't be read),
  all of these refuse; the main VM's `start`, `stop` and `status` don't depend on it.
  The main VM and `MCOMP_CONFIG` VMs may still share the account's own vault folder and
  spool, as before. Folders you set in `config/local.env` or an `MCOMP_CONFIG` file are
  trusted where nothing is deleted (SECURITY.md, "More than one VM");
- in a checkout with no `config/local.env` and no `--vm`: any command that acts on a
  VM while `config/vms/` has files, or the runner has a VM besides the default one,
  or its list can't be read; and an `MCOMP_CONFIG` file that doesn't exist. `help`,
  `where`, `setup`, `vm`, `list`, `default`, `events`, `notify`, `learn` and `actions` still work there;
- a settings file that can't be read cleanly (readable, parses, nothing on stderr while it's
  read, including from a file it sources): never taken for the defaults; and a `config/vms/`
  VM, a `vm delete` or a `build` while another VM's settings file can't be read;
- `install.sh` with `MCOMP_VM` set (it sets up `config/local.env`'s VM), and `setup`
  for a VM chosen by name;
- any command a hub runs (its reconnects, `input-reset`) when its settings no longer
  name the VM it started with (its file gone, or another VM, port or host key): a hub
  serves only that VM (`hub stop` ends it).

### An agent session's computer

An agent session (Claude Code or Codex) reaches a computer two ways: through the metacomputer MCP tools, and through
the commands it runs in a shell. The tools act on the computer the session's MCP server started on (your default
computer at that moment), or on the one the session chose with `select_computer`. Each command picks its own: `--vm`,
`MCOMP_VM`, `MCOMP_CONFIG`, else the default computer as it is now. The two can differ: the default changed after the
session started, or a login shell put another `metacomputer` first on PATH ([codex.md](codex.md#login-shells-and-path)).

**The MCP tools.** Besides Cua Driver's tools, the MCP server has two of its own, answered on the Mac (they work while
every computer is off):

| Tool | Arguments | What it does |
| --- | --- | --- |
| `list_computers` | none | Every computer, as `metacomputer list --json` has them: `name`, `place`, `where`, `state`, `default`, `settings`, and `this_session` (the one this session's tools act on); `default`, `this_session`, `selected`, `problems` and a `message` for the whole list. Changes nothing |
| `select_computer` | `name` (required): a computer's name | This session's tools act on that computer from then on, through its own hub (one hub per computer), until the session ends or it selects another. The computer before lets go of the session (its claim, its driver session), as when the session ends: only what that connection holds, so a claim a later connection in the same name wrote stays, and the MCP server waits for it before it exits. Starts and stops nothing. The result says whether a command that names no computer reaches this same one (its name, SSH port and folder), and the command to use for it in the shell: bare when it does, else one that names it (`metacomputer --vm NAME`, or its own `MCOMP_CONFIG` file when its settings are outside this checkout). Refused while a call is still running, for a name this checkout has no settings for, and for a computer with `MCP_HUB=off` other than the one the session started on. Refused too when the hub of the computer it would leave is from an earlier metacomputer (its hello doesn't say `conn_claims`, so it can't tell this session's connections apart): nothing changes (this session's connection to that hub stays as it is, so the hub sees no disconnect it could mishandle), every switch away from that computer stays refused while that same hub process runs, and replacing it is the user's call: `metacomputer hub stop` once nothing else needs it (that disconnects this session too, and its next call starts a current hub); then select again. It never stops or restarts a hub itself. A signal only asks the MCP server to end: it ends from its own main loop, once any switch or close under way is done, waiting for the computer it left; from then on it opens no connection and answers new calls with an error |

**The guard.** Run from an agent session, a command that names no computer and would act on another computer than
the one the session holds refuses before it does anything, saying which and how to go on:

```
metacomputer: this Codex session's MCP tools act on demo, but this command would act on metacomputer (config/local.env's VM, as the command names none). Nothing was done.
- To act on metacomputer anyway, name it: ~/.local/bin/metacomputer --vm metacomputer sh ...
- To act on demo, this session's computer: ~/.local/bin/metacomputer --vm demo sh ...
- Or move this session to metacomputer: the metacomputer MCP tool select_computer with name metacomputer; then commands that name no computer act on it.
```

- **What the session holds:** the computer its MCP tools act on, from the start of the session (the one selected,
  after `select_computer`); a session without metacomputer's MCP server holds the computers it has a claim on
  (`metacomputer sh`, `call`, `claude` and scripts claim the computer they act on).
- **Not checked:** a command that names its computer (`--vm NAME`, `MCOMP_VM`, `MCOMP_CONFIG`, or a caller that pinned
  it); commands that act on no one computer or name their own (`list`, `vm`, `default`, `each`, `volume`, `refdisk`,
  `events`, `notify`, `learn`, `actions`, `agents`, `setup`, `update`, `version`, `where`, `help`); forms that only
  read files and settings on the Mac (`checkpoints`, `recordings` without `--sync`, `record moments`, `browser` and
  `browser --json`, `hub env`, `net` and `net status`, `trace list|show|export`, and the vault's `secrets` (status),
  `secrets list|status|audit|tokens|requests|selftest|settle|version`, `secrets rule list` and `secrets audit-sink
  list|show`); what a command
  that passed starts for the same computer; anything run outside an agent session.
- **Forms that only read the VM, or who controls it** (`who`, `status`, `inspect`, `logs`, `get`, `shot`, `view --print|--tailnet`, `hub`
  and `hub status`, `claude read|wait|status`, `handoff status|wait`, `security` without `apply`, `record` and `record
  status`, `browser profiles`, `input-reset --check`) go ahead, with a note on stderr naming both computers. A form is
  read from the arguments as the command itself parses them, in whatever order they come (`security --json apply`
  applies, so it's checked as acting); one that isn't a read form acts.
- **Records.** The MCP server and each claim leave a small record of the session's computer in a private folder of this
  account's (`metacomputer-sessions` in the account's own temporary folder; `MCOMP_SESSIONS_DIR` moves it), so a command
  from another checkout sees them too. Each names the session's process and the one that holds the computer (the MCP
  server, or the claim's holder) by process id and start time, and counts only while those same processes are there: the
  MCP server still in that session, the claim still in that computer's owners folder in the session's name. A claim
  keeps the session's start time from when its holder took it (a hub's lease for calls a session left running keeps it
  through every refresh), so a later process that reuses the pid isn't tied to it; a claim without one (written before
  they were kept) can't be shown to be any session's, so it doesn't count, and nothing gives it one later. The folder
  must be this account's and writable by no one else (permissions are never changed to make it so: one that isn't is
  left as it is and not used, and a command says so); one `MCOMP_SESSIONS_DIR` names gets no records when it is or holds
  a VM's folder, the home folder or the checkout, checked again for every write, which goes through the folder opened as
  the one checked (a folder or link replaced after the check gets nothing). Only files that are records written here are
  ever removed (those of sessions that have ended, or that no longer stand), through `lib/safedelete.py`. When the
  process table can't be read, the command goes on and says so. Any program running as you can write a record, so, like
  the other checks against your own account, this stops a session acting on the wrong computer by accident, not a
  program set on it (`lib/sessionvm.py`).

**The VM** (all but the names take effect on the next build):

| Key | Default | |
| --- | --- | --- |
| `VM_NAME`, `BASELINE_NAME` | `metacomputer`, `metacomputer-baseline` | Lume VM names |
| `VM_CPU`, `VM_MEMORY` | `4`, `8GB` | VM size |
| `BROWSERS` | `firefox chrome` | Browsers built in; `firefox` alone leaves Chrome out; add `webkit` for GNOME Web (WebKitGTK, for visual checks) |
| `BROWSER_EXTENSIONS` | (none) | Extensions for every Firefox and Chrome profile, at the next build: `ublock` (uBlock Origin; uBlock Origin Lite in Chrome), `consent-o-matic` (says no to cookie banners). Each is fetched at its latest version when the browser first starts. Firefox rejects the cookie banners it knows either way |
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
| `RUN_DIR`, `LOG_DIR` | `.run/`, `logs/` in the checkout (`.run-vms/NAME`, `logs-vms/NAME` for a `config/vms/` VM) | Runtime files and logs; a second VM needs its own |
| `VNC_PASSWORD_STORE` | `file` (a new install writes `keychain`) | `keychain` (the login Keychain, item `metacomputer-vnc`), `file` (0600 in `SSH_DIR`), or `command` with `VNC_PASSWORD_GET` / `VNC_PASSWORD_SET` for your own secret store |
| `VIEW_CLIPBOARD_S` | `45` | How long `view --open` leaves the live-view password on the clipboard; it's cleared then only if the clipboard still holds it (`0`: never) |
| `VIEW_REMEMBER` | `ask` | Screen Sharing's remembered password (the login keychain item it reads for the live view). `ask`: the first time, at a terminal, `view --open` asks (a no sets `off`); once saved, each `view --open` keeps it up to date. `on`: save without asking. `off`: no writes at all (nothing saved or updated; `view --remember` refuses); the clipboard instead. Any other value counts as `off` |
| `INTERACTIVE_PROMPTS` | `off` | Allow metacomputer to show dialogs on this Mac that wait for you. `on`: a handoff Claude opens shows one on the Mac's screen, with an Open live view button (never over SSH or without your screen session), and `secrets ask` also shows a dialog with a hidden field for the one-time code (read when you unlock). `off`: nothing pops up by itself. A command without a terminal never prompts or opens a dialog itself, whatever this says |
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
| `BROWSER_PACING` | `human` | `human`: typing, batch steps and pixel clicks aimed at a Firefox or Chrome window go at a person's pace (a random 60-140 ms per key, 120-320 ms between steps, the pointer moved toward a pixel click first; at most 15 s added per call; a `delay_ms` the call sets is kept; [BROWSER.md](../skills/metacomputer/BROWSER.md#human-pacing-browser_pacing)); `fast`: as fast as the driver goes. `metacomputer browser pacing` changes it |
| `BOT_CHECK` | `on` | `on`: a window read that shows a human check (reCAPTCHA's or hCaptcha's box, Cloudflare's "Verify you are human" or "Just a moment...", press-and-hold, DataDome, Arkose, Google's "unusual traffic") gets the hub's note to hand it to you, sends `needs_you.captcha`, and acting calls aimed at that window are refused (`bot_check`) until a whole read of it (no `query`, no `since`) no longer shows it, or any read after a handoff; `note`: the note and the event, no refusal; `off`: neither ([SECURITY.md](../SECURITY.md#human-checks-on-screen-bot_check)) |
| `UPDATE_CHECK` | `on` | `on`: `metacomputer status` says when a newer release is out, from a check at most once a day, in the background: it asks the project's public GitHub repository for its release tags, without credentials, and only for a checkout of the project; `off`: never checks |
| `HIDDEN_TEXT` | `on` | `on`: page text a person likely can't see (too small, off the page, no size on screen) or that addresses AI agents is marked in the results that carry it, for Claude to tell you about, and reported as `security.hidden_text` ([SECURITY.md](../SECURITY.md#hidden-page-text)); it labels, never removes or blocks; `off`: results as the driver sends them |

**Lessons** (`metacomputer learn`; [docs/learn.md](learn.md)):

| Key | Default | |
| --- | --- | --- |
| `LESSONS` | `on` | `on`: the lessons you approved go in every new session's MCP instructions, as advice; `off`: kept, not sent |
| `LESSONS_DIR` | `~/.local/share/metacomputer/lessons` | Their private store (a 0700 folder, 0600 files, read only when private) |

**One-time codes** ([SECURITY.md](../SECURITY.md#one-time-codes-and-sign-in-links-ask-give-code-relay)):

| Key | Default | |
| --- | --- | --- |
| `CODES_VIA` | `vault` | `vault`: Claude uses `secrets code`, `relay` or `ask` and never reads or types a one-time code itself. `claude`: your opt-in to let it (the code lands in the transcript, and nothing ties it to the site). Guidance the skill follows, not enforced |

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

**Recordings** (`metacomputer record`, and per-task recordings in task folders):

| Key | Default | |
| --- | --- | --- |
| `RECORD_FPS` | `20` | Frames per second |
| `RECORD_QUALITY` | `standard` | `high`: sharp enough to edit into videos, several times larger |
| `RECORD_TO` | `host` | `host`: stream to the Mac while recording, so what arrived is out of the VM's reach and nothing builds up in it. `guest`: record in the VM and copy at `record stop`; until then anything running there can delete it, and `record start` warns ([SECURITY.md](../SECURITY.md#recordings)) |
| `RECORD_DIR` | `recordings/manual/` | Any folder on the Mac, such as an external drive; only its last part is created |
| `RECORD_AUDIO` | `auto` | `auto`: include the VM's sound when it has the virtual sound card; `off`: video only |
| `TASK_SYNC_INTERVAL` | `15` | Seconds between copies of a task's finished recording turns to the Mac while its task folder is open (`task start` to `task finish`); each turn is copied once and `task finish` never replaces it. `0`: copy only at `task finish` |

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
`~/Library/Application Support/metacomputer/` (`MCOMP_VAULT_DIR`). A VM in
`config/vms/` has a vault folder and spool of its own (`VAULT_DIR`,
`EVENTS_SPOOL`; [More than one VM](#more-than-one-vm)); the sinks are this Mac's,
shared by every VM, and each event names its VM.

## The Claude Code plugin

`./install.sh` sets Claude Code up in two parts, both for every project:
- the plugin `metacomputer@metacomputer`, at user scope, from the marketplace
  `metacomputer` (your checkout). It brings the `metacomputer` skill
  ([skills/metacomputer/](../skills/metacomputer/)), which says when a task belongs on
  the virtual computer and how to drive this desktop; Cua Driver's own guide, as
  `metacomputer:cua-driver`; `/metacomputer:learn`, which runs the lessons report in a
  chat; and its `bin/metacomputer`, the `metacomputer` command on Claude Code's PATH.
  The plugin declares no MCP server and no hooks.
- the MCP server `metacomputer`, at user scope (`claude mcp add -s user metacomputer
  ~/.local/bin/metacomputer mcp-lazy`), which lists the tools even while the VM is off
  and reaches the VM through the hub. It runs the checkout the `metacomputer` command
  runs, so `--switch` moves both. Claude Code loads a server it just added after a
  restart, or `/mcp` in an open session; the install says so when it added one.

**An MCP server already named `metacomputer`.** The installer reads Claude Code's
user-scope entry exactly (`lib/claude_mcp.py`): from the file Claude Code itself uses
(`~/.claude.json`, or `.claude.json` in `CLAUDE_CONFIG_DIR`; its older `.config.json`
there while Claude Code still uses that one), before and after `claude mcp get`, whose
output from an empty folder, where no project or local server can stand in for it,
must match it. When they disagree, either can't be read, or the entry changes while
it's being read, nothing is changed.
It then goes by the entry's type, its command (every link followed) and its exact
argument list:
- `~/.local/bin/metacomputer` with exactly `mcp-lazy`: kept as it is, variables you
  set on it included.
- this checkout's own launcher (a path that resolves to its `bin/metacomputer`), or a
  path earlier versions registered for this checkout (`bin/metacomputer` and the
  names before it), with exactly `mcp-lazy` and no variables: repaired, and the
  install says what it ran.
- another metacomputer checkout's launcher with exactly `mcp-lazy`: moved to this one
  only with `--switch` (or a yes at a terminal).
- anything else: never removed or changed, at a terminal or not, with `--yes` or
  without. That includes `~/.local/bin/metacomputer` with other arguments (a space
  more or less too), an interpreter or another file inside the checkout, a path that
  only starts with the checkout's, a launcher that doesn't exist (nothing shows whose
  it was), one that sets variables of its own, and one that isn't a local command. The
  install says how to remove it yourself (`claude mcp remove metacomputer -s user`,
  then `./install.sh --only-claude`), and its Claude Code step counts as unfinished.
  The install and `metacomputer agents` name such a registration by its program and
  how many arguments it has, or its transport: its arguments, variables' values and
  URL are never printed, since any of them can carry a credential.

Only metacomputer's own registration is ever removed, and it's read again right
before that and left as it is when it changed since (variables included: another
Claude Code may change it meanwhile; Claude Code has no lock an installer can hold
across the removal and the add, so only a change in the moment between that read and
the removal can go unseen). Once it's removed, the new one is always added and read
back; when that doesn't work, the install says the server is missing (or that it
can't tell) and the exact command that adds it, `claude mcp add -s user metacomputer
~/.local/bin/metacomputer mcp-lazy`: nothing of yours is lost, since that command
makes metacomputer's again. What served the tools before is retired only once the new
server is registered and read back: an older plugin copy's own server, another
checkout's marketplace and its plugin (read first: a plugin you turned off stays off,
and one that can't be read stops the switch), the plugin itself with `--no-plugin`,
and the server names earlier versions used; each removal is checked, and one whose
state can't be read is left as it is and counts as not done. A marketplace other
than your checkout and the project's repository is named only as another checkout's
or another source's, never by its folder or location. A project or local server
named `metacomputer` in the folder you run the install in takes precedence there:
it's reported, never changed. When a lookup fails, the server can't be registered,
or something can't be retired, the rest of the install still runs, it says what to
run, and it ends with exit status 1; it also records the Claude Code step as
unfinished, right after that step (`--only-claude` and `--only-agents` too; a run
that finishes it clears that, and a run that doesn't set Claude Code up leaves it),
so the next `metacomputer update` runs the install again even when the checkout is
already at the release. When the registration doesn't work and the plugin brings no
server of its own (the checkout it loads from no longer declares one), the install
says plainly that new Claude Code sessions have no metacomputer tools until that's
fixed, gives the line that fixes it, and says that `metacomputer update` tries again.
`--uninstall` removes metacomputer's server before the command it runs, and keeps the
command (`~/.local/bin/metacomputer` and `mcomp`) while any MCP server that stays,
Claude Code's or Codex's, metacomputer's or not, runs it.

**Tool names.** `mcp__metacomputer__*`, the same as in Codex. Through v0.4.4 the plugin
brought the server itself and the tools were `mcp__plugin_metacomputer_metacomputer__*`:
permission rules, scripts and MCP configurations of your own that name the long
prefix need the short one, for example
`"permissions": {"allow": ["mcp__metacomputer__*"]}`. The skill, the benchmark and
`metacomputer learn` accept both. `./install.sh --no-plugin` sets up the same server
with a skill link at `~/.claude/skills/metacomputer` instead of the plugin; an earlier
install's skill link is removed when it points at the same checkout and the plugin is
there, so the skill isn't listed twice.

**Updates.** Installed from the checkout, the plugin loads in place: after a
`git pull`, new sessions pick up the new skills. Installed from GitHub
(`/plugin marketplace add metavert-io/metacomputer`, then
`/plugin install metacomputer@metacomputer`; on Claude Code 2.1.275 or later,
`/plugin install metacomputer --marketplace metavert-io/metacomputer` does both),
Claude Code keeps a copy that follows the repository's commits: update it with
`claude plugin update metacomputer@metacomputer`. That copy's `bin/metacomputer`
runs your checkout's, through `~/.local/bin/metacomputer` or the checkout
`MCOMP_HOME` names; with no checkout yet, it can only run `setup`
([install.md](install.md#from-claude-code-first-the-plugin)), whose install registers
the MCP server. A copy from before that still brings its own server is updated by
`./install.sh` (`metacomputer update` runs it), only once the user-scope server is
registered and read back; when that update fails, the install says the tools are
listed twice until you run `claude plugin update`.

**The plugin updated on its own.** Through v0.4.4 the installer left the MCP server
to the plugin. A plugin from GitHub updated to this version while the checkout keeps
its old installer has no tools: nothing registered the server. The plugin's
`metacomputer status` (and `agents`, `where`, `help`, `version`) says so, and so does
the skill: run `metacomputer update`, which moves the checkout to the release whose
installer registers it, then restart Claude Code. The old checkout's `./install.sh`
doesn't add it.

**Whose plugin.** A marketplace named `metacomputer` that's already registered is
used only when it's this checkout or github.com/metavert-io/metacomputer. From
anywhere else, its plugin isn't installed, since it would run that source's code
on your Mac; the installer says how to remove it and sets up the skill link and
server instead.

**Turned off.** A plugin you turned off stays off, and the install leaves the rest of
Claude Code's setup as it is: it adds no MCP server then (after `claude plugin enable
metacomputer@metacomputer`, `./install.sh --only-claude` adds it). `metacomputer agents`
says which parts are there.
