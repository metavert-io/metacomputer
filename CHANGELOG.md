# Changelog

All notable changes to metacomputer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `tests/test_doc_claims.py` (no VM, about a second) pins the docs' security promises to the code and settings that keep them, so SECURITY.md, the README and the guide can't promise more than the code does: the vault's approval defaults and the identity-provider list, the API proxy's limits, what recordings show of a filled login, the optional hardening's defaults (in `config/defaults.env` and in the code's own fallbacks), the hub's limits and what the coordinate guard checks, the gateway's refusal list both ways, mcomp-vm's flags, pinned versions and digests, every row of the settings tables, and no absolute security claims anywhere. Each promise is one row of a table: the document, its phrase, and the check (TESTING.md).
- A benchmark task set for measuring lessons (`bench/tasks-lessons/`, eight tasks; bench/README.md, "The lessons set"): ordinary desktop work chosen so the stumbles `metacomputer learn`'s lessons address come up, from the failure classes `learn report` found in recorded sessions and the app notes in DESKTOP.md. A seat map drawn on a canvas, totals across Calc sheets, a chart set to an exact size, a copy saved in Excel format, a library page whose buttons say "Extend" when you asked to renew, facts that are link text in a long article, two find-and-replace passes in Mousepad, and a control that no built-in lesson answers (Calc's AutoCorrect turning a typed `->` into an arrow). Each task names the failure patterns it targets, the built-in lessons that answer them and where the pattern was seen, and is scored from the VM's real state; no task needs a lesson to pass. Run it with `metacomputer learn bench --tasks lessons` or `bench/bench.py run --tasks lessons` (`--tasks` now takes set names as well as task ids).
- Benchmark reports count failure patterns with `learn report`'s own classifier, run over each task's transcript: failed driver calls by class, route churn, a failure class coming back, and acting on an unverified result. `summary.md`, `compare` and `learn bench`'s printout show them, each with how many tasks target it, plus each task's targeted count; `run.json` records which lessons the sessions got (`lessons_ids`). `bench/bench.py export RUN_ID` writes a run's public summary for `bench/runs/` (no paths, costs or transcripts). The checks gained readers for sheets by name, chart frame sizes and `.xlsx` files.
- The first comparison on the lessons set, with three built-in lessons approved (`no-frame`, `repeat-class`, `not-found`), shows no measurable effect: every task passed both ways (8/8), and pixel actions without a screenshot frame went from 3 to 0 and failed driver calls from 7 to 3, single-digit counts from one run of each (docs/learn.md, `bench/runs/learn-lessons-2026-11-18-*.json`).
- `scripts/release.sh VERSION [--dry-run]` prepares a release through a pull request: it refuses unless it's on an up-to-date, clean main, the CHANGELOG's [Unreleased] has entries, `scripts/check-public.sh -a` passes and every no-VM suite passes; then it moves [Unreleased] to [VERSION] - DATE on a branch `release-vVERSION` and opens the PR (never a push to main). After the merge, `scripts/release.sh VERSION --tag [--draft-release]` tags the merge commit, pushes the tag and checks that get.sh's dry run now installs it; `--draft-release` drafts a GitHub Release from the CHANGELOG section without publishing it (`tests/test_release.py`).
- The hub-served sign-in tools are back, as Cua Spaces' Keyvault names them: `request_site_login` (sign in to a site in page-mode Chrome with a saved login, the vault typing the values and the hub submitting) and `teleport_browser_session` (load a session imported from your Chrome, only the named sites' cookies), plus the SDK's `Hub.request_site_login(url, options)` with `SiteLoginOptions`, `SiteLoginReport` and `SiteLoginError`. They run after the hub's one-controller check, bind the vault's call to what the hub checked (`--origin`, the Chrome's `--browser-pid`, `--site`), return no page address (only `same_site`), and ask as the hub itself (no session or project). docs/moving-from-spaces.md maps them; skills/metacomputer/SECRETS.md says when Claude uses them and when `secrets login`.
- `mcomp-vault`: `--while-pid PID` on `fill`, `login`, `session` and `token` withdraws the request when that process ends (the hub passes its own); `login --agent NAME` shows and records what the asking agent calls itself (1 to 64 plain characters, unverified). A request from the hub has no Claude Code session or project and must name the Chrome it checked.
- `lib/vmtaint.py`: the mark that says the VM may hold a value from the vault (`RUN_DIR/vm-may-hold-values`).

### Changed
- The install no longer shows the linker's harmless `ld: warning: search path '...' not found` lines from the Swift builds of mcomp-vm and the vault (Apple's Command Line Tools name SDK folders they don't have). Every other line those builds print is still shown, and a failed build still stops the install.
- SECURITY.md states the API proxy's limit on an answer: 600 seconds from sending the request, after which it's cancelled and the connection to the VM closed.
- Once a value may have reached the VM, nothing the VM chooses is kept on the Mac until it's reset (SECURITY.md, "After a value reaches the VM"). `metacomputer secrets fill|login|session load|token` and the sign-in tools mark the VM before the vault can send anything; `metacomputer reset` and a new build clear the mark. From then on:
  - the action log records every call, answer and refusal as `secret_adjacent` (tool, client, argument names and size class; failed or not, an image count, a text size class; a refusal's code), whatever `ACTION_LOG_TEXT` says;
  - the driver connection's stderr is withheld;
  - hub.log names exceptions by type only;
  - the hub neither asks for, saves nor serves a new tool list, handshake or resources and prompts listing;
  - `metacomputer sh` records output streams by size class only.
- `metacomputer secrets fill|login|session|token` refuse `--vm` naming a VM other than the config's (use `MCOMP_CONFIG`).
- A sign-in or session load holds the computer until its work has provably stopped (its vault call answered or `settle` returned; every driver call answered or its connection's process tree ended). That holds while its client stays connected, under the client's lease if it leaves, and across the hub stopping. The hub doesn't idle out, or restart for another client's settings, while one runs. architecture.md, "Tools the hub serves itself", lists every way a job ends. `metacomputer hub stop` waits up to 75 s for them.

### Fixed
- `metacomputer learn bench` stops with the reason when a benchmark run is refused (another session using the test VM, the cost cap, a usage limit), and says how to pick it up again, instead of starting the second run and printing an empty comparison ("over the 0 tasks"); two runs with no task in common print no table.
- `metacomputer learn approve` prints its summary once (`Lessons: 1 active in the MCP instructions, ...`), not "active in the MCP instructions" twice.
- A desktop recording's key moments (the description file `record stop` writes, and `record moments`) no longer list calls that didn't run as if they did: a call the action log records as refused is marked `refused` (a guard on the Mac, or a shell run refused after its call was logged), and one that came back as an error, Cua Driver's own refusals in bounded mode included, is marked `error`.

## [0.2.0] - 2026-11-18

The first public release. Highlights:
- **One-line install:** `get.sh` sets up everything, VM included, in about 5 minutes on a Mac with Homebrew and Apple's Command Line Tools.
- **The vault:** approvals by Touch ID, imports from Chrome, the Passwords app, 1Password and Bitwarden, and the same verbs as Cua Spaces' Keyvault.
- **Storage:** disk volumes, shared Mac folders (visible in Finder), S3-backed folders and reference disks.
- **Lessons:** `metacomputer learn`, from Claude's own sessions.
- **The hub:** one connection per VM, with every guard on every path.
- **The plugin:** the skill and tools as a Claude Code plugin.
- **Plus:** the shell channel, the event stream and the benchmark.

### Added
- `metacomputer learn` (docs/learn.md): the virtual computer learns from your own sessions. `learn report [--since 7d] [--project PATH] [--json]` reads this Mac's Claude Code sessions that used the metacomputer tools (either prefix; Claude Code's session files and saved headless transcripts), plus action logs, the event log and benchmark results, and reports failed driver calls by class (malformed arguments, each hub guard's refusal, element not found, pixel actions without a screenshot frame, and more), `unverifiable` results acted on without a check, failure classes that repeat within a session, runs of changed routes without re-checking the premise, and time per app. It proposes short lessons with their evidence from built-in templates, and with `--draft-with-claude` from one headless Claude Code session that gets only counts. `learn review`, `approve`, `reject`, `list`, `remove`, `edit` and `add` manage them; approved lessons live in a private store outside the checkout (`LESSONS_DIR`, default `~/.local/share/metacomputer/lessons`) and reach every new session through the MCP instructions, after the skill's habits, as advice that no guard reads (`LESSONS=off` stops that). Every change made through `learn` is recorded with hashes in `approvals.jsonl` and its event, and lessons are sent only while the store matches that record and the record's last change has its own event, the latest for that store (kept in a never-rotated `lesson-events.jsonl` as well as the event log): a lessons file written another way, an entry added by hand or a record cut back isn't sent until `learn reseal`, at your terminal, accepts it (best-effort detection; an event sink off the Mac is the copy your account can't rewrite, and lesson events skip the family rate limit so a sink that's down gets them late rather than never). `metacomputer status` and `security` count them and warn about a mismatch, including one with the event log, and the hub's status reports the store's hash. Approving, editing, removing or resealing emits `security.lesson_approved`, `lesson_edited`, `lesson_removed` or `lessons_resealed` (an id, a short title and two hashes, never the text). Approved lessons are model context: they go to the model with each session's instructions. `learn bench --config TEST_ENV` runs the benchmark on a test VM without and with them and compares pass count, invalid calls, turns, time and cost; it asks first, since it spends your Claude usage. Its first run, with two built-in lessons approved, passed 19/20 both ways ($9.67 without, $9.81 with; 16.6 turns against 17.1): no measurable effect, since neither lesson's pattern came up in the benchmark's tasks.
- The plugin's `/metacomputer:learn` command runs the report in a chat and walks you through the proposals.
- The benchmark's `--lessons on|off` switch (`BENCH_LESSONS`, default off).

- S3 volumes (docs/storage.md, "S3 volumes"): `metacomputer volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P]` keeps a folder volume's local copy in step with a bucket on AWS S3, R2, MinIO or any S3-compatible store, through `rclone bisync` on the Mac at start, every `S3_SYNC_INTERVAL` (60 s) while the VM runs, and at stop. Conflicts keep both copies (`name.conflict1.ext`); overwritten local files are backed up; `volume test` checks the endpoint, the keys and bucket versioning; `volume sync` runs one now. Keys go into the macOS Keychain through `volume keys` (macOS's own prompt) and reach rclone only in its environment: never arguments, `rclone.conf`, logs, events or the VM. rclone is pinned by version and SHA-256 behind its pinned release-signing key (`scripts/fetch-artifacts.sh rclone`). Events `task.s3_conflict` and `task.s3_sync_failed`.
- Folder volumes (docs/storage.md, "Folder volumes"): `metacomputer volume create NAME --folder PATH` shares a Mac folder into the VM over virtiofs, visible in Finder on the Mac. Read-only by default (held read-only by the Mac, and checked from inside the VM at every start); `VOLUMES=NAME:rw` makes it read-write for one VM at a time. While a VM has a folder read-write, a watcher on the Mac puts macOS's quarantine attribute on everything it writes (the exact value is enforced: measured, the VM can remove or rewrite the attribute through virtiofs, and a rewritten "user approved" value lets an unsigned binary run), replaces links that lead out of the folder with a note, removes FIFOs and devices, and sends `security.symlink_neutralized`, `special_file_removed` and `quarantine_restored` events. A read-write folder must have `.noindex` in its path, which keeps Spotlight out (measured on macOS 27: `.metadata_never_index` no longer does). Personal folders, Library and iCloud Drive, dot-folders and this checkout are refused.
- Storage that outlives the VM (docs/storage.md). **Disk volumes** (`metacomputer volume create NAME --size 20G`, `list`, `resize`, `delete`): ext4 images on the Mac, attached read-write to one VM at a time with `VOLUMES=name[:mountpoint]`, formatted inside the VM on first use and only when blank, locked while a VM runs, kept across `reset`, `restore` and `build --replace`. **Reference disks** (`metacomputer refdisk create NAME --from FOLDER`): read-only UDF images of a Mac folder that any number of VMs attach with `REFDISKS`. Mount points can be `/volume`, `/mnt`, `/data`, `/srv`, `/workspace` or the VM user's own folders, so scripts written for Cua Spaces keep working. `STORAGE_DIR` (or `--dir`) puts images on any drive, with a warning for read-write volumes on a network filesystem. `checkpoint NAME --with-volumes` snapshots volumes with the VM (APFS clones when it can) and `restore` rolls them back; `start --no-volumes` starts with nothing attached; in bounded mode only what the manifest lists is attached. `status`, `security` and the event stream (`lifecycle.storage_attached`) show what's attached. A section in docs/storage.md maps Cua Volume's concepts to these. VMs need this version's build (`metacomputer build --replace`); an older VM with storage configured refuses to start and says so.
- One-command install: `get.sh` (`curl -fsSL …/get.sh | bash`) checks the Mac and the free space where the VM and the checkout go, offers Homebrew's installer (downloaded whole before it runs) and Apple's Command Line Tools dialog when they're missing (it never handles a password), clones into `~/metacomputer` at the latest release (main before the first) and checks that the checkout is the commit the repository named, runs `./install.sh --yes`, then checks the VM: a VM it just built is started and gets a quick self-test; a VM you already had gets only read-only checks (`status` and a look at its windows; started only if it's stopped), so nothing open in it is closed or typed into. A failed release lookup stops instead of falling back. Numbered steps with time estimates, `--dry-run`, `--confirm`, `--replace-vm`, safe to run again. [docs/install.md](docs/install.md) is the install guide, with times measured on a fresh setup (about 5 minutes with the prerequisites in place).
- `metacomputer setup`: from the Claude Code plugin with no checkout yet, it runs the get.sh shipped in the plugin (explains and asks first; `--yes` from Claude's shell). It never deletes anything: an existing checkout is used only when it's a clean one of the project's repository with nothing but its data and value-only settings, and the host tools and mcomp-vm are built from new clones. In a checkout, `metacomputer setup` is `./install.sh` (with no terminal, only with `--yes`).
- The live-view password goes in the login Keychain for a new setup (`VNC_PASSWORD_STORE=keychain`, item `metacomputer-vnc`), written with `security -i` fed on stdin, updated in place and verified by a one-time tag, within a bounded wait; a Keychain that's locked or missing falls back to a 0600 file written through a private folder with no-follow, exclusive opens. Existing setups keep their store. The password now reaches the VM on ssh's stdin (within a time limit) instead of through an environment variable.
- A Claude Code plugin, `metacomputer`, with a marketplace in the repository (`.claude-plugin/marketplace.json`, the plugin in `plugin/`). It brings the `metacomputer` skill, Cua Driver's guide as `metacomputer:cua-driver`, and the MCP server, to every project. Install it in Claude Code with `/plugin marketplace add metavert-io/metacomputer` and `/plugin install metacomputer@metacomputer`; the VM still comes from a checkout and `./install.sh`, which also installs the plugin. A plugin installed from GitHub runs your checkout's `bin/metacomputer` (through `~/.local/bin/metacomputer`, or `MCOMP_HOME`).
- `./install.sh --only-claude` runs just the Claude Code step.
- `metacomputer secrets`: a self-contained vault for website passwords, 2FA (TOTP) codes and sealed browser logins. Claude fills them into page-mode Chrome without the values passing through its context, the transcript or the logs. Once typed, a value is on the page: passwords show as dots, but usernames and one-time codes are visible in screenshots and recordings. You unlock it in your own terminal; by default uses then go ahead without asking (sealed logins ask), and `--approval each` or `unlock --confirm each` makes each use wait for you to type the secret's name there (which a program that can type into that terminal could also do). VMs built with this version have a locked fill account that Claude can't modify. See SECURITY.md for exactly what holds and what doesn't.
- API keys through a proxy: `metacomputer secrets set NAME --kind api --api-host HOST` stores a key, and `secrets token NAME [--ttl 15m]` gives a program in the VM a short-lived token for a proxy at `127.0.0.1:18765` inside it. The proxy adds the real key on the Mac, sends requests only to that host, and withholds the key if the API echoes it back. Tokens last at most 8 hours and end when the vault locks; `secrets tokens` lists them and `secrets revoke ID|--all` ends them. VMs built with this version have a locked proxy account for it (rebuild older VMs to use tokens).
- Secure Enclave binding: `secrets init` binds the vault to this Mac's Secure Enclave when it has one and prints a one-time recovery key. `secrets recover` opens the vault with that key on another Mac; `rekey --enclave` and `rekey --no-enclave` move a vault onto or off the Enclave; `init --no-enclave` makes a passphrase-only vault.
- Rotation: `secrets set ... --replace` records a rotation and `secrets list` shows each secret's age; `secrets rekey` changes the passphrase and every key (the old recovery key stops working); `secrets rotate-session-key` re-encrypts sealed browser logins.
- The vault's audit log is hash-chained, and each record the vault writes carries a MAC under an audit key kept inside the vault (`secrets audit --verify`, `audit --head`); every event is copied to macOS's unified log, and `scripts/verify-audit.py` checks a log or a sink's capture. Audit sinks (`secrets audit-sink add`) send every event off the Mac as it happens, as signed webhooks, OTLP logs, syslog over TLS or ntfy messages; a `--required` sink must acknowledge each use before it goes ahead, and a record that fails the vault's checks reaches the sinks as an `audit_tamper` notice instead.
- Requesters and approval scopes: the unlock terminal and the audit log name the Claude Code session and project behind each request. When a use needs approval, typing `NAME session` or `NAME unlock` extends it to that session or until the vault locks, and `set --project DIR` limits a secret to sessions in that folder. This is attribution, not isolation.
- The skill's `SECRETS.md`: how Claude signs in, keeps a login and uses API tokens, and what it must never do.
- Vault approvals from anywhere: every use that waits for you has a request id. `secrets requests` lists them (who asked, the site, a masked username, the wait), `secrets approve ID` allows one with Touch ID or your login password (macOS's own dialog, shown by the unlock process) or `--passphrase`, and `secrets deny ID` refuses one. `secrets unlock --touch-id` shows the dialog for every question by itself, and lets you approve inside tmux or screen. The unlock terminal stays the default way to answer.
- Unattended rules: `secrets rule add --secret NAME | --site URL | --session NAME --project DIR --for 8h` lets that use happen without asking, for Claude Code sessions in that folder, until it expires (at most 30 days); making one takes Touch ID or the passphrase. `secrets rule list` and `rule remove ID`. `secrets policy NAME --approval session|each` changes a secret's approval without typing its value again.
- Presence mode for vault approvals: every approval and every `NAME session` / `NAME unlock` grant needs Touch ID (or your login password in macOS's dialog) or the vault passphrase, and a typed answer at the unlock terminal only chooses how far it reaches. `secrets unlock --require-presence` / `--no-require-presence` switch it for one unlock, `secrets presence on|off|auto` (passphrase) keeps a setting in the vault, and `secrets status` and `metacomputer security` show the mode in force.
- `secrets login NAME --password-selector CSS [--username-selector CSS]`: a username and a password into the page's fields under one approval.
- For people moving from Cua Spaces' Keyvault (docs/moving-from-spaces.md): the same verbs and the audit log's site-login event names; Claude signs in with `metacomputer secrets login` / `fill` under the same approvals (the `request_site_login` and `teleport_browser_session` MCP tools come in a point release). `secrets lock` locks from any terminal; `init --passphrase` and `unlock --passphrase` are accepted (`--passphrase-stdin` is refused). metacomputer shares no code with Spaces.
- Imports: `secrets import-passwords --browser chrome [--profile NAME]` reads Chrome's saved passwords (a copy of its Login Data, decrypted with the "Chrome Safe Storage" key that macOS's Keychain gives the vault after asking you), and `--csv FILE` reads an export from Chrome, the macOS Passwords app (its OTPAuth column becomes a TOTP secret), 1Password or Bitwarden. One secret per site and username, updated in place on a re-import; `--site` narrows it, `--dry-run` shows what would come in. Imported logins start at `each` approval, and the output shows only sites, counts and masked usernames.
- `secrets import-session --browser chrome --site HOST --name NAME --to-profile P`: a site's cookies from your own Chrome, sealed in the vault, then loaded into a VM profile's own cookie store by `session load` (re-encrypted with the VM's Chrome key, through the same guarded path and approvals as any sealed session). Cookies only; localStorage isn't imported. Cookies that reach an identity provider need `--allow-identity-provider` and ask at every load. `session load NAME --site HOST` sends only the cookies those sites get, filtered on the Mac and named in the question and the audit record.
- Imports read Chrome's databases as files someone else may have prepared: the tables must be ordinary tables (not views or virtual tables), within row, size and time limits. Cookie stores must be version 24 or later (Chrome 130), and a cookie whose encrypted value names another domain than its host stops the import. An imported 2FA code is recorded as its login's own (`totp_for`), a re-import replaces only that one, and every code made or replaced is audited. A login keeps at most its first 20 sites.
- The vault's audit records carry the site-login event names other password vaults use, beside their own `op`: `login.request`, `login.authorize`, `login.fill` and `login.denied` (field `event`), with a masked `username_hint` and the `request_id`. New records `request` and `authorize` mark when a use waited for you and how you allowed it. Webhook sinks get them as the CloudEvents extension `mcompevent`, OTLP sinks as `metacomputer.event`; existing logs and their chain verify as before.
- The event stream (`metacomputer events`, `metacomputer notify`; docs/events.md), off until you add a sink: Claude blocked on you, finished tasks and recordings, security signals (blocked connections, Chrome-guard and bounded-mode refusals, a failed `actions verify`, `web open-untrusted` findings) and the VM's lifecycle and health, sent to webhooks (CloudEvents, signed), OpenTelemetry (OTLP/HTTP), syslog over TLS or ntfy. Every string is cleaned and floods are rate-limited; sinks change only from your own terminal, and any change to them is announced to the existing sinks. The skill's `EVENTS.md` says when Claude notifies you.
- The shell channel, `metacomputer sh` (docs/shell.md): bash in the VM as the desktop user, with the desktop's environment, returning output, exit code and duration. It treats the VM as hostile (bounded output, one deadline, a strict reply schema, escaped terminal output), counts as an acting call for the one-controller rule, is off while `DRIVER_MODE=bounded` (`VM_SHELL`), and goes in the signed action log when it's on (`SHELL_LOG`). The skill's `SHELL.md` routes work by where its state lives, after Microsoft Research's Hybrid Computer Use work.
- File tools in the default VM: LibreOffice Calc, and for scripts python3-openpyxl, python3-docx, python3-odf, python3-pil, poppler-utils, sqlite3, zip and unzip, and python3-venv and pip. The bounded `desktop` profile launches Calc by name.
- A benchmark (`bench/`, bench/README.md): 20 desktop tasks run in fresh headless Claude Code sessions on a test VM, scored by checks that read the VM's real state, with switches to compare one change at a time. The first baseline passed all 20 for $11.95. `bench/runs/` holds each cited run's per-task results (pass, invalid-argument calls, turns) with its model, driver version and commit.

### Changed
- get.sh's and install.sh's notes about GitHub logins now speak of private forks: the repository is public, so a plain clone works.
- Storage, after a second audit (docs/storage.md, SECURITY.md "Storage"):
  - S3 volumes sync in two stages: rclone works only on a private sync copy in metacomputer's
    state folder, and metacomputer exchanges it with the folder the VM sees through
    descriptors (no link followed, plain files only, each new file quarantined before it gets
    its name there). A local copy that was replaced isn't synced; a sync reserves it; rclone's
    sandbox now denies every write outside its state folder.
  - Free space is claimed per operation, so concurrent syncs can't spend the same bytes;
    rclone is stopped if the disk falls below the reserve; backups are pruned before a sync too.
  - Overlap rules are symmetric and apply within one start; reference disks take a record;
    folders holding images can't be shared; images are never opened through a link; `--dir`
    can't put an image in a folder volume; checkpoint delete takes its turn with save and
    restore, and `volume delete` holds the volume until its registry entry is gone.
  - The watcher stops the VM before it reports, counts entries as it lists them, bounds each
    pass in time, and has a supervisor that stops the VM if the watcher dies or stalls and
    checks free space twice a second. A storage failure at start powers the VM off from the
    Mac without asking the guest; an ordinary stop's 90 s deadline holds whatever the guest
    does.
  - Links in a read-write folder are kept only when their text can't lead out (no absolute
    targets, `..` only at the start and within the link's depth).
  - Manifest keys are read exactly as quoted; odd keys grant nothing.
  - Copies that can't be clones are sparse-aware, bounded to the size checked and to the
    space above the reserve (reference disks, checkpoints, S3 exchanges).
  - Registered paths are recorded as the kernel spells them and walked one folder at a
    time without following links; images and S3 local copies are bound to the device and
    inode recorded when made. New images get the same checks and reservation in
    `STORAGE_DIR` as with `--dir`. S3 files are made and quarantined in a staging folder
    beside the shared copy (exclusive, identity-bound, kept out of every VM's view; cleanup
    removes only names it recorded), then renamed in. `volume delete --keep-image` keeps
    the image file. Checkpoint operations read their manifest under
    the checkpoint's lock before reserving its volumes. Space is claimed per APFS container.
    Files changed on both sides with the same size and time are compared by content.
- `./install.sh` asks one question instead of up to four (it shows what it will do; Enter means yes, `--yes` skips it): mcomp-vm, Chrome and building now are the defaults, changed with `--lume=standard`, `--no-chrome` and `--no-build`. It refuses before changing anything when Homebrew, the Command Line Tools, free space (12 GB where Lume keeps VMs, 3 GB for the checkout) or the network are missing, or when the VM runner can't list the VMs; numbers its stages with estimates and timings; and says what to do for each failure. A rerun never replaces a VM: one without its clean snapshot (an interrupted build, by the build's own marker, or not) is replaced only with the new `--replace-vm`, which `--yes` doesn't imply. It honours `MCOMP_CONFIG` like every other command. The build finds the VM's folder through Lume's storage settings (failing closed) and checks it's the new VM's before replacing its disk, and an interrupted Ubuntu image download resumes. One install runs at a time per user, and one change to each VM (build, reset, restore, baseline, checkpoint) at a time: flock locks on a descriptor every child inherits, so a lock lasts as long as the work; a second run waits briefly, then refuses. The host tools are rebuilt whenever they don't match their pinned sources, whether or not the VM is built. Free space is checked on every disk the run writes to, and where Lume keeps VMs is never guessed. `--no-build` leaves any VM as it is and succeeds.
- mcomp-vm 0.7.2-mcomp.2: the runner now introduces itself as mcomp-vm (banner, usage, `--version`), from metacomputer-vm's fresh public history. `install.sh` rebuilds an installed runner that isn't the pinned release.
- `metacomputer put` checks every copied file by SHA-256 (on the Mac before sending, in the VM after) and puts nothing in place if any differs. It no longer overwrites: a name that's taken lands as `name (1).ext`, `name (2).ext` and so on, and `--overwrite` replaces instead. Scripts that relied on `put` overwriting (the benchmark, the Chrome tests) now pass `--overwrite`.
- `./install.sh` installs the plugin by default and removes the skill link and user-scope MCP server that earlier versions registered for the same checkout. The tools are then named `mcp__plugin_metacomputer_metacomputer__*` instead of `mcp__metacomputer__*`; update permission rules or scripts of your own that name the old prefix. `./install.sh --no-plugin` keeps the earlier setup and names. The skill, the benchmark and the tests work with both.
- Cua Driver 0.53.0 in the VM (was 0.52.1); rebuild a VM to get it. The same 65 tools and arguments; on Linux it no longer re-sends input that may already have landed, joins the accessibility bus `AT_SPI_BUS_ADDRESS` names, presses Enter for a trailing newline in `browser_type` on a single-line field, and refuses a trusted `browser_click` whose point another element covers (`browser_input_trust_unavailable`). The driver's skill pack in `.claude/skills/cua-driver/` and the cached tool list follow it.
- The coordinate guard also checks `press_key` and `hotkey` when they carry x,y (they click that point to focus it first), as it already did `type_text`.
- The skill, tightened from recorded sessions: a one-round start (`who`, one ToolSearch, only the notes the task needs), core habits in SKILL.md (input sent isn't proof, two tries then check the premise, reads chosen by the next action, batch shape, file dialogs, `metacomputer sh -c`), an error table in DESKTOP.md, and the notes for Inkscape, Audacity, Kdenlive and games moved to EXTRA-APPS.md. The bridge's MCP instructions repeat the core habits. On the benchmark (bench/README.md, "Costs"; per-task records in bench/runs/): the first full run with the new skill passed 19/20 with 1 invalid-argument call, against 14 before. After a further change to the notes, the three tasks it touched ran again and passed with none; with those results, 20/20 pass, turns 20.2 → 18.0 per task and cost $11.83 → $10.38, over two runs combined rather than one full run.
- After a successful build, the VM's private SSH host key is deleted from the Mac; only the public key and `known_hosts` are kept.
- `metacomputer secrets set` on a name that already exists now refuses unless you add `--replace`.
- Claude Code installed with the native installer (`~/.local/share/claude/versions/X.Y.Z`) is now recognized as Claude Code, so a session's `metacomputer sh` and `call` claims go with that session instead of a parent process that outlives it.
- The desktop user's display name, shown in the VM's panel, is now `metacomputer` instead of "Cua automation" (new builds).
- The VM runner is now mcomp-vm, formerly lume-secure, built from github.com/metavert-io/metacomputer-vm (tag v0.7.2-mcomp.1, commit pinned), because Cua's trademark policy asks a modified version to take another name. It's the same Lume build with the same flags, made by Metavert, not by Cua. `install.sh` installs it as `~/.local/bin/mcomp-vm` and takes `--lume=mcomp-vm` (`--lume=secure` still works); `LUME` defaults to it, and its pins are `MCOMP_VM_REPO`, `MCOMP_VM_TAG` and `MCOMP_VM_COMMIT` (`LUME_SECURE_*` in a local.env still count). Existing installs keep working: while `LUME`'s runner isn't installed and `~/.local/bin/lume-secure` is, metacomputer uses that one and prints a notice; `./install.sh` installs mcomp-vm beside it, points `LUME` there and leaves lume-secure in place. `install.sh --uninstall` names the runner in use.

### Security
- rclone (S3 volumes): the program inside the pinned zip is pinned too (`RCLONE_BIN_SHA256` in
  config/defaults.env). Before every use the unpacked program is checked against that value; a missing
  or different one, a planted copy with a matching digest file included, is never run but unpacked
  again from the zip once the zip matches its own pin. No digest file is written or trusted any more.
- Lessons can't carry text from the VM into later sessions: proposals are built only from classes, counts, the driver's own tool names (anything else is `unknown_tool`) and app names from a fixed list; `--draft-with-claude` gets only counts; drafted or edited text, `learn edit`, `learn add` and `learn reseal` need a terminal, and review approves only the text it showed; the store is read only when private (yours, not group- or world-writable, not a link) and sent only while it matches the record of changes; each lesson is one cleaned line and the block is capped; transcripts are read with size, event and time bounds, and one bad file doesn't stop the report; and the hub reads lessons only to build instruction text, per client. A program running as you can still approve a lesson: SECURITY.md, "Lessons from your own sessions", says what is visible and what isn't.
- The benchmark checks the VM again before every reset, refuses the VM your own settings name, makes every command it starts refuse a VM other than the one it found (`MCOMP_EXPECT_VM`), and `metacomputer reset` itself checks the VM, the baseline and the settings file's hash in the process that replaces the VM. A session that reports no cost counts at its whole budget, and a reservation left by a runner that died is charged on every resume until a record settles it.
- Presence mode is on by default on a Mac with Touch ID and a finger enrolled (a default change for those Macs): a line typed at the unlock terminal no longer approves on its own there, since any program that can type into that terminal could type it. Macs without Touch ID keep typed approval as the default, and `unlock` warns once with how to turn presence mode on.
- Whole browser profiles saved with `secrets session save` ask at every load: `session policy NAME --approval session` and unattended rules no longer cover them, since a profile may hold an identity provider's sign-in. Replacing a session keeps its identity-provider mark.
- A grant or an unattended rule decides only whether a fill asks: every fill that might ask is checked in the VM first and then bound to the site that check reported, so a rule for one site can't send a value to another of the secret's sites.
- `--on-behalf-pid` is attribution only: shown as "(unverified)" and recorded as `requester.on_behalf` with `verified: false`, it never satisfies a `--project` limit, an unattended rule or a grant (it was taken as the requester before).
- After a value or a session has reached the VM, failures are reported in the vault's own words from the exit status; the VM's error output no longer reaches results, the audit log or the unlock terminal.
- Audit records are bounded (long lists are cut at 32 items, and a record over 64 KiB keeps only its core fields), and the next record always links to the last one, however long it is.
- `secrets login|fill --origin https://site` fills only on that one of a login's sites, and `session load --site HOST` sends only the cookies those sites get (filtered on the Mac).
- How far a vault approval reaches is part of what's confirmed: macOS's dialog and the passphrase prompt name the scope (this one use, this Claude Code session, until the vault locks), the passphrase proof covers it, and the grant is exactly that. `approve ID` is one use unless `--scope session|unlock` asks for more; a scope typed at the unlock terminal while a dialog is open withdraws that dialog and opens one naming the new scope.
- Presence mode fails closed: it stays on when Touch ID can't be checked (a disconnected Touch ID keyboard, an error), and once a vault has seen Touch ID enrolled on the Mac it stays on until `presence off`. Only a clean "no sensor" or "no finger enrolled" on a vault that never saw Touch ID keeps typed approval as the default.
- `secrets login|fill --browser-pid PID` fills only in that Chrome, and fills refuse when two or more tabs there are on the site, instead of picking the focused or visible one.
- Imported 2FA codes are tied to their login's id (random, kept through `set --replace`, never reused), not its name: a later login with the same name never inherits another account's code. `delete` unties a deleted login's code and says so.
- Nothing the VM chose goes into a result, the audit log, the unified log, the sinks or the unlock terminal, on any request: the VM knows every value it was ever given, so a check that carries no value is held to this as much as a fill. The site only as one of the request's own sites, the character count as the vault counts the value, the route only as `element` or `keyboard`, the reason only as one of the vault's own sentences; the VM's error output and exit status are never shown. No number the VM can steer is recorded as it chose it: a sealed session's size and an API answer's size are kept as one of a few size classes, the API's status by its class (`2xx`), whether the key was withheld as yes or no, and a token's requests in words. The API proxy's audit records name the method as one of a few words and never the request's path or an unknown secret name.
- Unattended rules store exactly what was confirmed: the dialog shows the rule's secret, site or session, project and absolute expiry (a rule too long to show whole is refused there; `--passphrase` shows it in the terminal), and `rule add --passphrase` fixes the expiry when it shows the rule.
- `set --replace` with another username or other sites unties the login's 2FA code; 2FA codes tied by name (an earlier build) are tied by id when the vault opens. A vault file put back to an older copy can't drop presence mode silently: the unlock checks the audit log and keeps the stricter of the file's setting and the log's last authenticated one (its MAC and chain position checked), whether the file says off or auto.
- The vault stops a `fill`, `login`, `session` or `token` request whose requester goes away or withdraws it: its question is withdrawn, no further step starts, and a step in progress (its ssh) is killed. It then answers `cancelled`, the confirmation the client waits for. The client withdraws on SIGINT, SIGTERM or SIGHUP, when its wrapper dies, or at its deadline; with no confirmation it exits 5. `metacomputer secrets settle` waits until the unlock process has finished everything sent before it.
- Identity providers always ask (a deliberate tightening of the vault's defaults): a fill on Google's, Microsoft's, Apple's or Okta's sign-in pages, or an API token for one of their hosts, asks every time. A secret's `--approval session`, `unlock`'s default `--confirm session`, earlier `NAME session` / `NAME unlock` answers and unattended rules no longer cover it, and such an answer there allows that one use. The list is in SECURITY.md ("Identity providers always ask").
- The vault recognizes Claude Code from the native installer (`.../claude/versions/X.Y.Z`) as the requester, as the one-controller rule already did, so `--project` limits and the "Asked by" line apply to it.
- Every SSH connection the Mac makes to the VM now runs with no config file (`-F /dev/null`) and with agent, X11, port and tunnel forwarding and connection sharing turned off, so nothing in your SSH config can forward anything to the VM.
- Files copied from the VM (`task finish`, `get`, `recordings --sync`, the test harness) arrive as plain files and folders only: links, hard links, FIFOs and devices are left out and reported, and nothing is written through a link, so a VM can no longer plant a link named like a contact sheet and have `task finish` overwrite a Mac file through it.
- Those copies also stop, keeping the files that arrived whole, once they pass the VM's disk size (`VM_DISK_BYTES`), when the Mac would have less than `IMPORT_MIN_FREE_GB` free (default 10), or after 200,000 entries, so a VM can't fill the Mac's disk or memory.
- Google's APT repo stays limited to the pinned key. Chrome's package rewrote the repo entry we wrote (it rewrites `google-chrome.sources` whenever that file exists) to trust a keyring its own scripts write; the entry is now `metacomputer-google-chrome.sources`, and the build fails if any other entry for Google's repo appears, even after a run of Chrome's daily job. Rebuild a VM with Chrome to get it.
- Text from the VM or a web page is shown escaped by `inspect`, `browser`, `web` and `record moments`, as `metacomputer sh` already did (`--json` stays exact).
- The API proxy matches `--api-path-prefix` by whole path segments (`/v1` no longer allows `/v10` or `/v1-admin`); a request's head must arrive within 10 seconds and its token is checked before any body is read; each VM has its own 16 connection slots.
- `actions verify` requires each checkpoint to sign the record right before it and the final one to end the file, so forged records followed by a replayed final checkpoint no longer verify.
- The VM's Mozilla and Google package keyrings hold only the pinned key, even if the downloaded key file carries others.
- The benchmark runs in a fresh private folder and runs `claude` by an absolute path found before its shim folder goes on PATH.
- The hub's socket, when RUN_DIR's path is too long for one, lives in your own temporary folder (`/var/folders/.../T`) instead of the shared `/tmp`, and clients check before they send anything that the socket's folder is yours and private and that the process serving it runs as you, so another account on the Mac can't stand in for the hub.
- `metacomputer web`, the GUI test harness and the status probe go through the hub like every other client, so its guards apply to them; `web` and the tests claim the computer through it before they start (`web --override` releases other sessions' claims first, as `metacomputer release` does). A status connection only reads: the hub refuses its claims. `lib/mcp_client.py` opens the raw driver connection (`MCOMP_DIRECT_BRIDGE=1`) only for transport tests that ask for it.
- A client of any kind that disconnects while one of its acting calls is still running in the driver (a script that timed out, a session that closed mid-call) no longer frees the computer at once: the hub holds it for that call in a lease it keeps fresh until the call ends or the driver connection closes, and `who` shows it. Past the activity window the hub ends the connection that call was sent on (even one it no longer uses for new calls), with its whole process tree (SIGTERM so the action log's relay signs its end, then SIGKILL, the group's leader reaped only after that so no other process group can be hit), and only then lets go, so the call can't act after the computer is free. If the lease can't be written, the client's claim stays and the hub's own record of the lease still counts as another controller. An acting call another controller still has running counts as busy even when its claim file is gone.
- The Chrome guard knows which app an element token came from, so `set_value` or background input aimed at Chrome by a token alone is refused or sent in the foreground like any other. A token from a read the hub doesn't have is refused whatever else the call names (`element_token_unknown`), and so are a window id the driver doesn't list (`window_unknown`) and a pid, window id and token that don't all belong to one app (`target_conflict`): hints the hub can't resolve, or that disagree, never count as "not Chrome". A `run_steps` step that names no window is judged by the window it inherits from the last step that named one, as the driver runs it.
- In a `run_steps` batch, the coordinate guard refuses a pixel step that comes after a step able to move, resize, open or close a window or a dialog (a double click or a click with count 2, a click with Super, Alt or Meta held, a middle click, a drag, a press on the desktop frame, a click on a dialog's button or a window control (Maximize, Minimize, Restore) named by role and name, Escape, or keys with Super, Alt or Meta, a function key, or Ctrl+N/W/Q/O/S/P), since it can't look between steps (`pixels_after_window_change`). Plain clicks, typing, Return and Ctrl+A between pixel steps still pass (counting them would refuse 10, 3 and 2 of the 373 recorded benchmark batches; SECURITY.md says what's left); none of those batches is refused. Pixels checked against a window layout cut at its 256-window cap are refused (`too_many_windows`): with more than 256 windows open (minimized ones count) pixel input fails closed, while role/name and token targets and keyboard input keep working.
- What a hostile VM can make the Mac's side keep or do is capped beyond the size of each message: a repeated `tools/list_changed` starts one refresh at a time, a refresh stops after 50 pages, 2,000 tools, 16 MB, 60 s or a repeated cursor, and what the hub learns from results is capped; the driver connection's stderr keeps a 64 KB tail and at most 1 MB in its log; JSON nested more than 100 levels deep, or with more than a million values (counted outside strings, so text full of commas passes), isn't parsed, instead of stopping a reader or filling memory (hub, relay, SDK, direct client), and when its top-level id is one of the hub's calls that call gets an error at once (`answer_too_complex`; ids nested anywhere else never count, and strings are skipped in one linear pass); the direct client's backlog has a byte budget; screenshot layouts keep only checked geometry, at most 256 windows each and 32,768 in all. A driver that keeps sending messages the hub can't use (not JSON, or of the wrong shape) is hung up on after 50 in a minute on one connection, `hub.log` is rotated at 5 MB while the hub runs, and the same kind of note is written at most five times a minute, the rest counted in a summary line when the minute ends, the connection closes or the hub stops.
- Copies from the VM refuse tar extension headers over 64 KB, more than 8 in a row or more than 64 MB in all (before Python's `tarfile` reads them into memory), GNU sparse entries (also those a pax header marks sparse, before their map is read), headers with a negative size, and compressed streams, and don't keep the entries they've passed; count every file and folder they make, including folders a path implies, against a 200,000 limit and the free-space floor; and cap the whole stream. The GUI test harness streams its copy into the importer with the same limits as `get` instead of holding it in memory, and stops the SSH command when a limit is reached; a command that exits nonzero or a copy that fails is recorded in the run's errors and marks its evidence incomplete.
- On a terminal, the VM's words are escaped in the stderr of SSH commands, `inspect --json` (written again as ASCII-only JSON), `record start` (streamed, never held whole), `record status`, `status` (whose load, memory and disk figures show only as numbers), recorder messages and logs, `logs`, `who` and `input-reset`; `metacomputer ssh` stays raw. The host-mode recording's stream log is capped at 1 MB.
- `./install.sh` uses an already registered marketplace named `metacomputer` only when it's this checkout or github.com/metavert-io/metacomputer; from any other source its plugin isn't installed (it would run that source's code on your Mac), the installer says how to remove it, and it sets up the skill link and MCP server instead.

### Fixed
- `metacomputer reset`, `stop` and `checkpoint` go on when copying the VM's recordings fails: a VM whose home folder had been wiped couldn't be reset at all (the copy exited the whole command).
- `web compare`, `responsive`, `a11y` and `open-untrusted` work more than once per hub: each run's Chrome session gets a name of its own (a fixed name stayed ended on the hub's driver connection).
- The Claude Code plugin carries `LICENSE` and `THIRD_PARTY_NOTICES.md`, and Cua Driver's skill pack its own `LICENSE` (Cua's MIT notice, from the release), so the copy a GitHub install makes keeps them. The pack's README and RUNTIME guide no longer tell you to install the driver on your Mac, grant it permissions there or read guides this copy leaves out; they point to metacomputer's setup, and `scripts/vendor-driver-skill.py` makes those edits on every re-vendor.
- Docs: what the vault's approvals ask by default, what recordings show of a filled login, and what the coordinate guard can and can't catch now match SECURITY.md; the benchmark figures link per-task records of each cited run (`bench/runs/`); docs/install.md drops the instructions from while the repository was private.
- VM builds no longer stop at their last step ("stopping and cloning the clean baseline"): `vm_stop` and `vm_force_stop` in `scripts/lib.sh` guard the `kill`/`wait` of their background helper, which tripped `set -E` and the build's ERR trap whenever the helper had already exited (`tests/test_vm_stop.py`).
- Benchmark: `bold_runs` reports what Writer shows in bold. A bold paragraph style with the rest of its text in a not-bold span no longer counts as the whole paragraph bold, which failed `writer-format` runs whose documents were right (`tests/test_bench_checks.py`).
- `metacomputer who` failed with a traceback when a `metacomputer sh` claim from outside Claude Code was listed.
- `metacomputer status` failed with a JSON error when the MCP probe failed part-way.
- SECURITY.md's vault command table: `audit-sink test NAME` needs your terminal (and the passphrase) while the vault is locked.
- `./install.sh --help` shows its whole description again (the last step was cut off).
- Two VMs side by side: a session on one VM no longer makes the other refuse every tool call ("in use by another controller").
- A refused `build` (the VM exists, or a bad setting) no longer replaces the host key and `known_hosts`, which broke SSH to the existing VM.
- `metacomputer sh` outside Claude Code: its claim on the computer lasts the activity window even when the shell that ran it exits.
- The API proxy: a same-host redirect keeps the API's path encoding and trailing slash; after an answer times out, nothing more is written to that connection.
- Sealed browser sessions refuse at once, with a clear message, under `SSH_AUTH=cert` (they need `SSH_AUTH=key` for now).
- The event stream: an OTLP collector's partial success counts the rejected events as dropped; every delivery attempt has a wall-clock deadline, so a sink that trickles bytes can't hold `start`, `stop` or `sink add`; the relay reads `events.json` only when it changes.
- The README now says that `checkpoint --delete` asks first, like `reset`, `restore` and `baseline`, and that with no terminal to ask on it needs `--yes`.

## [0.1.0] - 2026-11-17

The first public release. It covers everything built from 2026-11-16 to
2026-11-17, including pull requests #1 to #5.

### Added

- **The virtual computer.** `metacomputer build` (`scripts/build-vm.sh`)
  builds an Ubuntu 24.04 ARM64 XFCE desktop as a Lume VM, unattended: Ubuntu's
  official cloud image, a cloud-init seed, provisioning of Xorg, LightDM
  autologin, AT-SPI accessibility, Firefox, LibreOffice Writer, GNOME
  Calculator, Thunar, Mousepad, a terminal, ffmpeg and a VNC server, then a
  clean baseline snapshot. Versions and digests are pinned in
  `config/defaults.env`.
- **Cua Driver 0.52.1 inside the VM,** reached by Claude Code over SSH as the
  `metacomputer` MCP server. `metacomputer mcp-lazy` answers the MCP handshake
  from a saved copy, so the tools are listed in every project even while the
  VM is off, and connects on the first call.
- **The `metacomputer` skill** (`skills/metacomputer/`): which computer a
  request means, when a task needs the desktop at all, a routine for tasks,
  and desktop (DESKTOP.md) and browser (BROWSER.md) rules verified on this VM.
- **The `metacomputer` command,** with the short alias `mcomp`: `start`,
  `stop`, `restart`, `status`, `reset`, `checkpoint`/`restore`, `baseline`,
  `who`/`release`, `inspect`, `put`/`get`/`shot`/`rename`, `task start|finish`,
  `contact-sheet`, `view`, `input-reset`, `desktop-restart`, `call`, `ssh`,
  `logs` and `where`.
- **`install.sh`**: checks the Mac, installs missing tools with Homebrew,
  installs Lume, writes `config/local.env`, links the command, the skill and
  the MCP server for every Claude Code project, and builds the VM.
  `--uninstall` removes the links and the MCP registration.
- **Google Chrome** as an optional second browser (`BROWSERS`), with three
  browsing modes (human, page and hybrid), defaults set by
  `metacomputer browser`, and named Chrome profiles
  (`metacomputer browser profiles`).
- **Browser checks that need no model:** `metacomputer web compare`,
  `responsive` (with `--baseline` difference images), `a11y` and
  `open-untrusted`.
- **Recordings:** per-task MP4s and per-action traces through Cua Driver, and
  whole-desktop recordings with `metacomputer record start|stop`.
- **One controller at a time:** a second Claude Code session's actions are
  refused while another session acted in the last 10 minutes, and `stop`,
  `reset`, `restore`, `baseline` and `test` refuse unless given `--override`.
- **Chrome input guard** in the bridge: input aimed at Chrome is sent in the
  foreground, `set_value` and `run_script` aimed at Chrome are refused, and the
  driver's leftover virtual input devices are removed before Chrome needs the
  keyboard.
- **`EXTRA_PACKAGES`** (#4): more Ubuntu packages built into the VM, such as
  Inkscape, LibreOffice Calc, Audacity or Kdenlive. Package names are checked
  on the Mac and again in the VM.
- **Recording settings** (#4): `RECORD_FPS`, `RECORD_QUALITY` (`high` is sharp
  enough to edit into videos), `RECORD_TO=host` to stream a recording to the
  Mac while it runs, and `RECORD_DIR` for any folder, such as an external
  drive. A missing parent folder is refused rather than created.
- **A description file beside each desktop recording** (#4): times, settings,
  length, size and SHA-256. With the action log on, it lists the key moments,
  each tool call with its time in the video; `metacomputer record moments`
  prints them.
- **A second VM side by side** (#4): `RUN_DIR` and `LOG_DIR` keep each VM's
  gateway, lock, bridge files and action log apart; select the other VM with
  `MCOMP_CONFIG`.
- **`VIRTUAL_AUDIO=on`** (#5): a sound card inside the VM that plays into
  nothing (PulseAudio with one null sink). `RECORD_AUDIO=auto` includes its
  sound in desktop recordings, delayed 90 ms to land within about 50 ms of the
  picture. `metacomputer status` shows the sound card.
- **Desktop rules for extra apps** (#5) in DESKTOP.md: Inkscape, Kdenlive,
  Audacity, LibreOffice Calc and SDL/pygame windows, plus GTK file-dialog and
  window-sizing behavior.
- **Tests:** the recorded GUI suite (`metacomputer test`, A to F), the Chrome
  guard test, recording tests, action-log tests, option tests for a test VM,
  and headless skill tests. See TESTING.md.

### Security

- **lume-secure by default** (#1): Lume 0.7.2 plus flags that leave out the
  host-sharing devices. The VM starts without your Mac's clipboard, audio and
  Rosetta unless `SHARE_CLIPBOARD`, `SHARE_AUDIO` or `SHARE_ROSETTA` is `on`.
  `install.sh --lume=standard` installs Cua's release instead, with a warning
  that it always shares them. `metacomputer status` reports what the VM
  actually has, read from inside it.
- **A host-side network gateway** (`cua-netgw`, built from Cua's source at a
  pinned commit): the VM reaches the public internet only, never the Mac, the
  LAN, a tailnet, link-local or cloud-metadata addresses, or outbound mail
  ports. Blocked attempts are logged.
- **Nothing listens off the Mac's loopback.** SSH and the password-protected
  live view are forwarded to `127.0.0.1` only; Lume's own VNC server is never
  started.
- **SSH by role:** keys only, a host key generated on the Mac and pinned before
  the first boot, and separate `admin`, `ops` and `mcp` keys. The `mcp` key can
  only start Cua Driver's MCP server.
- **Narrow accounts:** the desktop user has no sudo; after the build the
  maintenance account may run only a fixed-verb admin script, cloud-init is
  turned off and its cached user-data is deleted.
- **Verified downloads:** the Ubuntu image (signed checksums, pinned key and
  digest), the Cua Driver release (pinned SHA-256, checked again inside the
  VM), Lume (pinned checksum, or lume-secure's pinned tag and commit), and the
  Mozilla and Google repository keys (pinned fingerprints).
- **A smaller guest:** no snaps, no clipboard agent, the virtio sound driver
  blacklisted, no shared folders, Cua Driver and Lume telemetry off, browser
  telemetry and password saving off by policy, and automatic security updates
  without automatic reboots.
- **Optional hardening** (#3), all off by default so metacomputer behaves like
  Cua Driver on its own; `metacomputer security [apply]` shows and applies it:
  - `ACTION_LOG=on`: a signed, hash-chained log of every tool call
    (`ssh-keygen -Y sign`), with typed text redacted by default, and
    `metacomputer actions list|show|verify`;
  - `SSH_AUTH=cert`: a fresh key and a short-lived SSH certificate for every
    connection, named in the VM's auth log; the static keys stop working;
  - `DRIVER_MODE=bounded`: Cua Driver's bounded mode with a capability
    manifest (profiles `readonly`, `desktop` and `browser`, or your own),
    requested by its SHA-256, with a VM-side policy that refuses standard
    sessions while bounded.

### Changed

- **The project is named metacomputer** (#1, #2). The command is
  `metacomputer` (alias `mcomp`), the MCP server and skill are `metacomputer`,
  environment settings use the `MCOMP_` prefix, and paths inside the VM are
  `/opt/metacomputer` and `~/metacomputer-runs`. For checkouts from before
  this release, `install.sh` removes command links, skill links and MCP
  registrations left by the earlier development names (`vc`, `cua-lab`,
  `virtual-computer`, `vcomp`, `cua-linux`), and `metacomputer status` points
  out VMs built with an older layout, which need a rebuild.
