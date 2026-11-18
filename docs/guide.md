# Using metacomputer: what to ask Claude Code

This guide goes through what the virtual computer can do, one task at a time:
what to ask Claude Code, and the command behind it if you'd rather run it
yourself. The [README](../README.md) has the overview,
[install.md](install.md) the install, and [commands.md](commands.md) every
command and setting.

- [How-to guide](#how-to-guide)
- [Isolation and safety in detail](#isolation-and-safety-in-detail)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## How-to guide

With the plugin installed, ask in plain words. Requests that mention "a
computer", "your computer", "a virtual, disposable, sandbox or Linux
computer", a real browser or GUI app, or "record yourself" go to the VM.
Requests about your own machine ("my screen", "my Mac", "my Chrome") never
do. Most capabilities also have a command you can run yourself, shown under
each group.

### Start it and check on it

- "Is the virtual computer running? Is anyone else using it?"
- "Start your computer." / "Shut down the virtual computer when you're done."

```bash
metacomputer status          # --json for machine-readable output
metacomputer who             # which session is using it, and whether a recording is running
metacomputer start
metacomputer stop            # finishes a running desktop recording first; --force powers off at once
metacomputer restart
```

One Claude Code session controls the VM at a time. While another session has
acted in the last 10 minutes, a second session's actions and shell runs are
refused (reads still work), and `stop`, `reset`, `restore`, `baseline` and
`test` refuse unless given `--override`. Claude asks you before taking over;
`metacomputer release` hands the VM over.

### Use desktop apps

- "Use the calculator on your computer to work out 137 × 29."
- "On the virtual computer, write a short note in the text editor, save it as notes.txt and give me the file."
- "Open LibreOffice Writer, write a one-page memo with a Heading 1 title, save it as memo.odt and copy it to my Mac."
- "Make a small budget in LibreOffice Calc with a SUM row and save it as budget.ods."
- "Open the file manager on your computer and tell me what's in Downloads."
- "Copy the first paragraph of this page in Firefox into a Writer document and save it."

Claude works from the accessibility tree: it finds controls by role and name,
batches the steps it has decided on, and verifies the outcome (a value in the
app, a file on disk) rather than trusting a screenshot. It takes a screenshot
only when the tree can't answer, such as for canvases, images or layout.

### Work with files through the shell

- "Convert these Word documents to PDF and zip them."
- "Count the rows per category in this CSV and give me the totals."
- "What's in this archive? Don't extract anything."

`metacomputer sh` runs a script in the VM as the desktop user, with the
desktop's environment, and returns its output, exit code and duration. Claude
routes work by where its state lives: files no app holds open go to the shell;
a document open in an app is changed in that app, because the app's copy is
what gets saved. It says when part of a task went through the shell, and never
writes a file behind an app's back to fake a GUI result.

```bash
metacomputer sh -c 'ls -la ~/Downloads'
metacomputer sh --file ./convert.sh -- ~/Inbox/reports        # a script from your Mac, with arguments
metacomputer sh --json -c 'pdftotext -layout ~/Inbox/invoice.pdf - | head -40'
metacomputer sh --read-only -c 'ls ~/Documents'                # a check that changes nothing
```

- `--timeout SECONDS` (default 120, at most 3600) stops the script and everything in its process group. `--max-output BYTES` (default 64 KB per stream, at most 1 MB) keeps the first and last part; the full size and SHA-256 are reported.
- Exit status is the script's own; 124 means it timed out, 125 that it was refused (`VM_SHELL`, bounded mode, another session), 255 that it couldn't run (VM off, SSH, a malformed answer from the VM).
- `~` and `$HOME` are the desktop user's home in the VM; nothing is expanded on the Mac.
- To start an app on the desktop from a script, detach it: `setsid -f mousepad FILE </dev/null >/tmp/mousepad.log 2>&1`.
- The VM has Python 3.12 with openpyxl, python-docx, odfpy, Pillow, PyYAML and requests; LibreOffice for headless conversion (`soffice -env:UserInstallation=file:///tmp/lo-convert --headless --convert-to pdf FILE`); `pdftotext` and friends, `sqlite3`, `zip` and `unzip`, `jq`, ImageMagick, ffmpeg, git and curl. `python3 -m venv` gives you pip for anything else, at user level.

[docs/shell.md](shell.md) has the design and
[skills/metacomputer/SHELL.md](../skills/metacomputer/SHELL.md) the routing Claude follows.

### Browse the web

- "Use Firefox to open example.com and follow its link."
- "Search Wikipedia for Ada Lovelace in Firefox on your computer and tell me the first sentence of the article."

Firefox is the default browser. Claude works it like a person: it reads the
page through the accessibility tree and clicks and types in the window.

### Use Chrome in page or hybrid mode

With Chrome installed, Claude can work a browser three ways:

| Mode | How | Browsers | Good for |
| --- | --- | --- | --- |
| **human** (default) | Reads the accessibility tree; clicks and types in the window like a person | Firefox, Chrome | Demos, recordings, UX checks |
| **page** | Works on the page's own elements through Chrome's tools, in the background; fastest and exact | Chrome | Forms, dropdowns, uploads, downloads, dialogs, reading a site exactly |
| **hybrid** | Reads and checks through the page; acts with real clicks and key-by-key typing that show in a recording | Chrome | Realistic recordings with exact verification |

- "In Chrome, drive the page directly and fill in the form at this URL with these details: …"
- "Upload report.pdf through the form on this page." (Claude copies the file into the VM first.)
- "Download the CSV linked on this page and give it to me."
- "Fill in the sign-up form in Chrome and show it typing, so it looks right in a recording." (hybrid)
- "Use Chrome like a person would." (human)

Page mode uses a throwaway Chrome profile unless you ask for a named one.
Asking for page or hybrid mode in Firefox gets an offer to use Chrome instead,
because Cua Driver has no page tools for Firefox.

```bash
metacomputer browser                    # the current defaults
metacomputer browser default chrome     # firefox | chrome
metacomputer browser mode page          # human | page | hybrid (page and hybrid need Chrome)
```

### Sign in to a site

- "Open a Chrome profile called shopping and wait while I sign in."

The VM holds none of your accounts, and Claude never types your credentials
itself. It opens the window and asks you to sign in through the live view
(`metacomputer view`), or uses the vault (next section). Later sessions with
the same profile name reuse its cookies until the VM is reset.

```bash
metacomputer browser profiles                    # named Chrome profiles in the VM
metacomputer browser profiles --delete shopping  # asks first
```

### Use saved passwords and 2FA codes

- "Sign in to the demo site as me with the `demo` login, then do X."
- "Log in to example.com; the code for its 2FA is in the vault as `example-otp`."
- "Keep this signed-in session so tomorrow's run doesn't have to log in again."

You set this up once, in your own terminal. Claude can't do these steps, and never
asks you for a passphrase, password or code in the chat:

```bash
./scripts/build-vault.sh                 # or ./install.sh, which builds it too
metacomputer secrets init                # creates the vault; asks for a passphrase; prints a recovery key once
metacomputer secrets set example --kind password --origin https://example.com --username you@example.com
metacomputer secrets set example-otp --kind totp --origin https://example.com    # the base32 seed
metacomputer secrets bind                # pins the VM; compare the fingerprint, then type yes
metacomputer secrets unlock --for 1h     # keep this terminal open while Claude works
```

Values are typed with the echo off, never on a command line. Write the recovery key
down and keep it away from this Mac. `set` on a name that already exists refuses unless
you add `--replace` (see [Rotate keys and keep an audit trail](#rotate-keys-and-keep-an-audit-trail)).

Or bring the logins you already have, in your own terminal:

```bash
metacomputer secrets import-passwords --browser chrome --dry-run   # what would come in: sites, masked usernames
metacomputer secrets import-passwords --browser chrome             # macOS asks about Chrome's key: Allow (once)
metacomputer secrets import-passwords --csv ~/Downloads/passwords.csv   # Chrome, Passwords app, 1Password or Bitwarden export
metacomputer secrets import-session --browser chrome --site github.com --name gh --to-profile work   # one site's cookies
```

Imported logins ask before every use until you relax them (`policy NAME --approval session`).
A one-time-code column in a Passwords app, 1Password or Bitwarden export comes in as a 2FA
secret too.
`import-session` brings a site's cookies, not its localStorage, sealed for a page-mode Chrome
profile in the VM; `session load NAME` puts them there (`--site HOST` sends only the cookies
that site gets). It needs Chrome 130 or later, whose cookie values name their own domain, and
stops if any value names another one. Delete a CSV export once it's in.

Then Claude:
1. opens the site in page-mode Chrome;
2. runs `metacomputer secrets login example --username-selector '#user' --password-selector '#pass'`, which fills both fields under one approval, or clicks into each field and runs `metacomputer secrets fill example --field username`, then `--field password`;
3. for the 2FA code, clicks into its field and runs `metacomputer secrets fill example-otp --field otp`.

How it works:
- **The unlock terminal:** it prints each request, who asked and the outcome, never the values. Close it, or press Ctrl-C, and the vault locks.
- **Approving each use:** add `--approval each` to `set` (or `--confirm each` to `unlock`), and every use waits for you in that terminal. It shows the secret, the site and who asked (for example `Asked by Claude Code in ~/Projects/foo`). What you type decides how far the answer reaches:
  - `NAME`: this use only;
  - `NAME session`: this use and later uses of that secret from the same Claude Code session;
  - `NAME unlock`: every use of that secret until the vault locks.
- **Approving from elsewhere:** each waiting use has a request id. `metacomputer secrets requests` lists them (who asked, the site, a masked username, how long it has waited); `metacomputer secrets approve ID` allows one with Touch ID or your login password, `approve ID --passphrase` with the vault passphrase, and `deny ID` refuses it. `unlock --touch-id` shows the Touch ID dialog for every question by itself. Nothing Claude can run approves: only your finger, your password or your passphrase does.
- **Presence mode:** on a Mac with Touch ID (and a finger enrolled), every approval and every `NAME session` / `NAME unlock` grant needs Touch ID (or your login password in macOS's dialog) or the passphrase; what you type at the unlock terminal only chooses how far it reaches, since any program that can type into that terminal could type it too, and the dialog or the passphrase prompt names that scope (`approve ID --scope session|unlock` asks for a wider one explicitly). It stays on when Touch ID can't be checked, and once the vault has seen Touch ID enrolled, until you turn it off. On a Mac without Touch ID, typed answers stay the default and `unlock` warns once. Switch it for one unlock with `--require-presence` / `--no-require-presence`, or keep a setting with `metacomputer secrets presence on|off|auto`; `secrets status` and `metacomputer security` show which is in force. A program that controls your terminal still sees the passphrase as you type it: only the system dialog defends against that ([SECURITY.md](../SECURITY.md#approving-from-elsewhere-request-ids-touch-id-the-passphrase)).
- **Unattended rules:** `metacomputer secrets rule add --secret NAME --project DIR --for 8h` (or `--site https://...`, or `--session NAME`) lets that use happen without asking for Claude Code sessions in that folder, until it expires (at most 30 days). A rule only decides whether to ask: the value still goes only to the site checked in the VM. Making one takes Touch ID or the passphrase; `rule list` and `rule remove ID` manage them. `policy NAME --approval session|each` changes whether a secret asks at all.
- **Identity providers always ask:** a fill on Google's, Microsoft's, Apple's or Okta's sign-in pages asks every time, whatever the secret, the unlock or a rule says (the list is in [SECURITY.md](../SECURITY.md#identity-providers-always-ask)).
- **One approval per sign-in:** `metacomputer secrets login NAME --username-selector CSS --password-selector CSS` fills both fields under one question.
- **Coming from Cua Spaces?** If you used its Keyvault, the names you know work here today: `metacomputer secrets requests`, `approve`, `deny`, `lock` and `import-passwords` (and `status`, `init`, `unlock`), and the audit log carries its event names. An MCP tool in the style of its `request_site_login` is coming in a point release; until then Claude signs in with `secrets login` and `fill`. [moving-from-spaces.md](moving-from-spaces.md) maps the rest and says what differs.
- **Limiting a secret to a project:** `set ... --project DIR` refuses requests from Claude Code sessions working outside that folder. Like the "who asked" line, this keeps honest sessions apart; it isn't a wall, since any program of yours can pose as a Claude Code session.
- **Keeping a login:** `metacomputer secrets session save NAME --profile P` seals a signed-in Chrome profile into the vault (it asks first), and `session load NAME` brings it back after a reset. A whole profile may hold any sign-in, so loading one asks every time and no rule covers it. For now this needs `SSH_AUTH=key` (the default): the vault signs in with the VM's static desktop key, which `SSH_AUTH=cert` turns off, so both commands refuse at once under `cert`. Fills and API tokens use their own accounts and work either way.
- **What Claude can see:** `metacomputer secrets list` shows names, kinds, sites, usernames, approval, project limits, ages and use counts, never values. `metacomputer secrets audit` shows the history.

Limits to know:
- Fills need the page-mode Chrome that Cua Driver starts, not Firefox.
- The VM needs to be built with this version, which adds the locked fill account. Older VMs say so and refuse.
- Usernames and one-time codes are visible on the page once typed; passwords show as dots.
- Unlock in a plain terminal window. Inside tmux or screen, another program could answer for you, so uses that need your answer are refused there unless you unlock with `--touch-id` (answer with Touch ID instead) or `--allow-multiplexer`.

The full model is in [SECURITY.md](../SECURITY.md#secrets-and-logins-metacomputer-secrets).

### Use an API key from the VM without the VM seeing it

- "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there."
- "Test the app in the VM against the real API, using the `openai` key from my vault."

You store the key once, in your own terminal. It goes in a request header (by default
`Authorization: Bearer <key>`):

```bash
metacomputer secrets set openai --kind api --api-host api.openai.com
metacomputer secrets set example-api --kind api --api-host api.example.com --header 'x-api-key: {value}' --api-path-prefix /v1
```

Then Claude asks for a token and points the program in the VM at the proxy:

```bash
metacomputer secrets token openai --ttl 15m   # prints the token, its id and base_url http://127.0.0.1:18765/openai/ (inside the VM)
metacomputer secrets tokens                   # the live tokens
metacomputer secrets revoke ID                # end one now (--all for every one)
```

The program in the VM sends its requests to that base URL with the token where the key
would go. The proxy on your Mac takes only well-formed HTTP/1.1 (or 1.0) requests, checks the
token and the path (a prefix such as `/v1` covers whole segments: `/v1` and `/v1/...`,
never `/v10` or `/v1-admin`), puts the real key in, and sends the request on to that API's host
only, over HTTPS. It never follows a redirect. If an answer contains the key, even
compressed, the proxy replaces it with `[key withheld by metacomputer secrets]`.

- A token lasts 15 minutes by default and at most 8 hours, and every token ends when the vault locks. Changing or deleting the secret ends its tokens too, so a token never picks up a new value or a wider scope.
- A token is a bearer credential: whatever holds it can call that API as you until it ends. It can't get the key or reach another host.
- The key is withheld only as an exact copy; an API that sent it back transformed (in base64, say) would hand it over.
- The VM needs the proxy account, which VMs built before API tokens don't have. `token` says so; rebuild the VM.

### Rotate keys and keep an audit trail

- "Which of my secrets haven't been rotated in a while?" (`metacomputer secrets list` shows each one's age)
- "Are the vault's audit sinks delivering?" (`metacomputer secrets audit-sink list`)
- "Check that the vault's audit log hasn't been changed." (`metacomputer secrets audit --verify`)

Rotating is up to you; nothing rotates on a schedule. These run in your own terminal
and ask for the passphrase:

```bash
metacomputer secrets set openai --kind api --api-host api.openai.com --replace   # a new value, recorded as a rotation
metacomputer secrets rekey                 # new passphrase, data key, Enclave key and recovery key
metacomputer secrets rotate-session-key    # re-encrypt every sealed browser login under a new key
```

**The Secure Enclave and the recovery key.** By default `init` binds the vault to
this Mac's Secure Enclave (when it has one) and prints a recovery key once. The
Enclave key never leaves the chip, so a copy of the vault file can't be opened
elsewhere with the passphrase alone.
- `metacomputer secrets recover` opens the vault with the recovery key, on another Mac or after this Mac's Enclave is reset, and sets a new passphrase and keys.
- `rekey --enclave` moves a passphrase-only vault onto this Mac's Enclave; `rekey --no-enclave` moves it off.
- `init --no-enclave` makes a passphrase-only vault, which opens on any Mac with the passphrase.

**The audit log.** Every unlock, lock, use, refusal, proxied API request and change is
one line in `audit.jsonl` in the vault folder, with names, sites, outcomes and who asked,
never values or tokens. Each line carries the hash of the one before it and a MAC
under the vault's own audit key, and each event also goes to macOS's unified log:

```bash
metacomputer secrets audit --tail 20
metacomputer secrets audit --verify        # every record's MAC and the chain, checked by the vault (while unlocked)
metacomputer secrets audit --head          # the current head, SEQ:HASH, to note at a moment you trust
python3 scripts/verify-audit.py ~/Library/Application\ Support/metacomputer/audit.jsonl
log show --predicate 'subsystem == "io.metavert.metacomputer.vault"' --last 1d
```

`verify-audit.py` recomputes every hash and checks the chain and the sequence
(`--expect-start` and `--expect-head` pin the ends). Your own account could
still rewrite the file and its hashes, but not the MACs: `audit --verify` names
any record the vault didn't write. Records that were deleted, or never
delivered before a rewrite, are simply missing, so copies sent off the Mac as
events happen are the record to rely on. There are four kinds of these
**audit sinks**, all optional:

| Kind | Sends to |
| --- | --- |
| `webhook` | Any HTTPS endpoint, one signed POST per event |
| `otlp` | An OpenTelemetry collector or service (OTLP/HTTP logs), signed |
| `syslog` | A SIEM or rsyslog, over TLS |
| `ntfy` | Your phone, through ntfy |

```bash
metacomputer secrets audit-sink add siem --kind syslog --url syslog+tls://siem.example:6514 --required
metacomputer secrets audit-sink add phone --kind ntfy --url ask     # the topic is typed, not echoed
```

- `--required` fails closed: each use is first recorded and must reach every required sink before it goes ahead. While one can't be reached, fills, tokens and session saves and loads are refused, and proxied API requests get 503.
- The vault signs a record for a sink only after it passes its own checks; a record that fails reaches the sinks as an `audit_tamper` notice instead, and the unlock terminal says `AUDIT LOG CHANGED`.
- `scripts/verify-audit.py --key KEY FILE...` checks what a sink received: signatures, hashes, chain and gaps (`--require-verified` also fails on records a locked client wrote).
- Adding or removing a sink needs your terminal and the passphrase. `audit-sink list` works from anywhere, though while the vault is locked it shows only each sink's delivery state, not its settings. `audit-sink test NAME` works from anywhere while the vault is unlocked; while it's locked, it asks for the passphrase in your terminal.

[SECURITY.md](../SECURITY.md#the-audit-log-and-sending-it-off-this-mac) has the event
fields, each sink's format, what each check proves, an OCSF mapping and collector
examples.

### Check a page in both browsers, at several widths, for accessibility

These checks are scripted: they need no model and write `report.md`,
`report.json` and images to a results folder.

| Ask | Or run |
| --- | --- |
| "Does this page look the same in Firefox and Chrome?" | `metacomputer web compare URL` |
| "Show me this page at desktop, tablet and phone widths." | `metacomputer web responsive URL --widths 1920,1280,768,500` |
| "What changed on this page since the last run?" | `metacomputer web responsive URL --baseline results/<earlier-run-folder>` |
| "Run an accessibility check on this page." | `metacomputer web a11y URL` |

- `compare` shows the two browsers side by side and lists differences in title, headings, links, controls and text. It needs Chrome.
- `responsive` and `a11y` use Chrome unless given `--browser firefox`. `responsive --baseline` adds similarity scores and difference images. `a11y` looks for unlabelled controls, images without text alternatives, missing headings and vague link text; Chrome finds more.
- Every `web` command takes `--task NAME` (write into that task's folder), `--record` (record the screen), `--json` and `--override`.

### Test a web app you're building

- "Build the site and click through the checkout flow on the virtual computer, in both browsers."

The VM can't reach your Mac, by design, so a dev server on your Mac isn't
visible to it. Claude builds the app as static files, copies the folder in,
and serves it inside the VM:

```bash
metacomputer put dist
metacomputer sh -c 'cd ~/Inbox/dist && setsid -f python3 -m http.server 8000 </dev/null >/tmp/http.log 2>&1'
# then open http://localhost:8000 in the VM's browser
```

### Open an untrusted link or file

- "Someone sent me this link. What does it do, and is it safe to open?"
- "Open this Word document on the virtual computer, not on my Mac, and tell me what it says."

```bash
metacomputer web open-untrusted URL              # --watch 10: seconds to watch for redirects
```

`open-untrusted` follows the server's redirects with `curl` inside the VM,
then opens the link in a throwaway Chrome profile and watches it. It reports
in-page redirects and tab changes, the final page with a screenshot, any
password fields (a sign that the page wants your credentials), and any
downloads it started, which stay in the VM. Nothing on the page is clicked or
typed into. Everything in the report came from the page, so it is treated as
untrusted. With an event sink, a password prompt, a download or a redirect to
another site is also sent as a security event.

### Record a task

- "Record yourself filling in this form in Chrome."
- "Use a disposable computer to write this memo in LibreOffice, and record the whole desktop while you do it."

There are two recorders:
- **Per task, through Cua Driver.** Claude makes a task folder and calls `start_recording`. It produces a 1920×1080, 30 fps MP4 and a folder per action with the state, screenshots and click markers before and after. `metacomputer task finish` copies it all to the Mac and makes a contact sheet.
- **The whole desktop, whoever drives it.** `metacomputer record start NAME` and `record stop` capture the screen with ffmpeg, at 20 fps by default. A description file (`NAME.json`) beside the MP4 lists its times, settings, length, size, sound track and SHA-256. With `ACTION_LOG=on` it also lists the key moments: every tool call and shell run made on this VM while it recorded, with its time in the video (for example `0:42 launch_app inkscape`).

```bash
metacomputer record start demo          # name and, optionally, frames per second
metacomputer record stop                # saves recordings/manual/demo.mp4 and demo.json
metacomputer record moments recordings/manual/demo.mp4
metacomputer recordings                 # recent recordings; --sync copies the VM's task runs to the Mac
metacomputer contact-sheet FILE.mp4     # 12 frames in one image
```

`RECORD_QUALITY=high` is sharp enough to edit into videos (several times
larger). `RECORD_TO=host` streams the recording to the Mac while it runs, so
nothing builds up on the VM's disk. `RECORD_DIR` can be any folder, such as
an external drive. Shell steps don't show in a recording, so for a recorded
demo Claude uses the apps.

Recordings capture the whole screen. Passwords filled by `metacomputer secrets`
show as dots, but usernames and codes are visible. With the driver's recorder,
Claude passes any other sensitive value it types to `redact_values`, or leaves
those steps out of the recording.

### Move files in and out

| Ask | Or run |
| --- | --- |
| "Here's data.csv. Open it on your computer." | `metacomputer put data.csv` (to `~/Inbox` in the VM; `--to '~/Documents'` for elsewhere). Every file is checked by SHA-256, and a name that's taken lands as `data (1).csv` unless you add `--overwrite` |
| "Give me the file you saved." | `metacomputer get '~/Documents/memo.odt'` (to `results/files/<time>/`, or `--task NAME`) |
| "Take a screenshot of the virtual computer." | `metacomputer shot` (to `screenshots/manual/`, or `--task NAME`) |
| "Rename that download to report.pdf." | `metacomputer rename GUEST_PATH report.pdf` |

Quote a VM path that starts with `~`, or your Mac's shell expands it to your
Mac's home folder.

For a task, Claude keeps one folder in the VM and one on the Mac:

```bash
metacomputer task start invoice-form    # prints NAME, RECORDING_DIR, FILES_DIR (in the VM) and HOST_DIR (on the Mac)
metacomputer task finish NAME           # copies everything to HOST_DIR, makes contact sheets, writes artifacts.json
```

With `ACTION_LOG=on`, `task finish` also copies the action logs written
during the task; the copies verify like the originals.

### Keep files across resets: volumes and reference disks

- "Keep my project files on a volume so they survive a reset."
- "Give the VM a read-only copy of this folder of reference papers."

```bash
metacomputer volume create work --size 20G        # a disk image on the Mac, sparse
metacomputer refdisk create papers --from ~/papers-for-vm   # a read-only image of a folder
# in config/local.env:
VOLUMES=work                 # in the VM at ~/data/work, read-write
REFDISKS=papers:/mnt/papers  # read-only, at a mount point of your choice
metacomputer restart         # attached at start
```

- A **disk volume** is read-write, belongs to one VM at a time, and is formatted inside the VM on first use (only if it's blank). It survives `reset`, `restore` and `build --replace`; `checkpoint NAME --with-volumes` snapshots it with the VM.
- A **reference disk** is read-only and any number of VMs can attach it.
- Mount points: `/volume`, `/mnt`, `/data`, `/srv`, `/workspace` or a folder in the VM user's home, so scripts written for Cua Spaces' `/volume` keep working.
- Images go in `STORAGE_DIR` (default `~/.local/share/metacomputer`) or `--dir`: an external drive or a NAS mount works too (read-write volumes on a network drive get a warning). A missing image refuses the start; `start --no-volumes` starts with nothing attached.

[docs/storage.md](storage.md) has the details and a table for anyone coming from Cua Volume.

### Share a folder with the VM (visible in Finder)

- "Put your results in a folder I can open in Finder."
- "Read the files in my project's `assets` folder, but don't change them."

```bash
metacomputer volume create assets --folder ~/work/assets            # read-only unless you say :rw
metacomputer volume create handoff --folder ~/vm-handoff.noindex    # read-write needs .noindex in the path
# in config/local.env:
VOLUMES="assets:/mnt/assets,handoff:/data/handoff:rw"
metacomputer restart
```

- **Read-only by default.** The Mac holds the share read-only, and the VM proves it at every start.
- **Read-write (`:rw`) is opt-in** and one VM at a time. While the VM runs, a watcher on the Mac puts macOS's quarantine attribute on everything the VM writes (so Gatekeeper checks it like a download, and puts it back if the VM removes it), replaces links that lead out of the folder with a note, and removes FIFOs and devices. The folder must be kept out of Spotlight with a `.noindex` name, because `.metadata_never_index` no longer works.
- Your personal folders (home, Documents, Desktop, Downloads, Library and iCloud Drive, `.ssh` and other dot-folders) and this checkout are refused, links resolved.

Details and what the watcher can't do: [docs/storage.md](storage.md#folder-volumes).

### Keep a folder in step with an S3 bucket

- "Use the project files in our team bucket, and put your results there."

```bash
metacomputer volume create team --s3 --bucket my-bucket --endpoint https://s3.us-east-1.amazonaws.com --prefix projects/x
metacomputer volume keys team      # in your own terminal: macOS asks for the keys (into the Keychain)
metacomputer volume test team      # endpoint, keys, bucket versioning
# in config/local.env:
VOLUMES="team:/mnt/team:rw"
metacomputer restart
```

The VM sees a folder; rclone (pinned and verified, on the Mac) syncs it with the bucket at start, every minute (`S3_SYNC_INTERVAL`) and at stop. A file changed on both sides is kept twice (`name.conflict1.ext`). The keys never enter the VM, a command line or a config file. Works with AWS S3, R2, MinIO or any S3-compatible store. Details: [docs/storage.md](storage.md#s3-volumes).

### Look inside the VM without the GUI

- "What's running on your computer right now? Is anything still downloading?"

```bash
metacomputer inspect                     # everything
metacomputer inspect windows ports       # sections: system windows processes ports files browser
metacomputer inspect files --minutes 30  # files changed in ~ and /tmp in the last 30 minutes
metacomputer inspect --json
```

`inspect` is read-only, so it works even while another session controls the
VM. It shows windows (with the ids the driver tools use), apps with their
helper processes, CPU and memory, listening ports, recent files, downloads
(`.part` means still running), the default browser and whether a recording is
running. Command lines are masked.

### Watch or take over

- "Let me watch what you're doing." / "I'll take over for a minute."

`metacomputer view` prints how to connect (`--open` opens Screen Sharing):
- **On the Mac:** `open vnc://127.0.0.1:5977`.
- **From another device:** tunnel over SSH (`ssh -N -L 5977:127.0.0.1:5977 <your-mac>`), then connect a VNC client to `localhost:5977`. If Tailscale is installed, `view` also prints the `tailscale serve` line that would publish it to your tailnet instead.

`view` also prints how to read the password, in your own terminal: from the
login Keychain (item `metacomputer-vnc`) on an install made with get.sh or
`./install.sh`, from `~/.ssh/metacomputer/vnc-pass` on an older one, or from
your own secret store (see `VNC_PASSWORD_STORE` under
[Configuration](commands.md#configuration)).

### Hear from it: notifications and events

- "Ping my phone if you get stuck at a sign-in, and when the task is done."
- "Send my metacomputer events to our Slack." (Claude gives you the command and the relay from docs/events.md.)
- "Is anything waiting to be delivered?"

Sinks decide what leaves your Mac, so you add them in your own terminal;
`events sink add` and `remove` refuse without one, and Claude doesn't change
them.

```bash
# in your own terminal: your phone, through ntfy (a long random topic; anyone who knows it can read it)
metacomputer events sink add phone --kind ntfy --url https://ntfy.sh/<long-random-topic> --families needs_you,task
metacomputer events sink test phone      # one test event, now
metacomputer events status               # sinks, the watcher, backlogs, failing sinks
metacomputer events tail                 # the local log
```

- When Claude is blocked on you (a sign-in, a CAPTCHA, a 2FA code, a dialog it won't accept, an approval) it runs `metacomputer notify "what it needs" --why login --action "what to do"` and says the same in the chat.
- The rest is sent automatically: finished tasks and recordings, security signals (blocked connections, the Chrome guard, bounded-mode refusals, a failed `actions verify`, `open-untrusted` findings), the VM's lifecycle, and a health probe every 5 minutes.
- Families: `needs_you`, `task`, `security`, `lifecycle`, and `actions` (every action-log record, for a SIEM; only when you name it). Filter per sink with `--families` and `--min-severity`.
- Kinds: `webhook` (CloudEvents, signed with HMAC-SHA256), `otlp` (to an OpenTelemetry Collector, which can forward to S3, CloudWatch, Datadog, Splunk, Loki and others), `syslog` (RFC 5424 over TLS) and `ntfy`. Use `--header-env 'Authorization=Bearer $TOKEN'` so a token stays out of your shell history, and give sinks ingest-only tokens: the sink file is readable by anything running as you.
- While a sink exists, `metacomputer start` also starts a small watcher process, and `metacomputer status` shows an `events` line.

[docs/events.md](events.md) has every event, the wire formats, a Slack
relay, a Collector configuration and an OCSF mapping.

### Save, restore and reset

| Ask | Or run |
| --- | --- |
| "Reset the virtual computer to a clean state." | `metacomputer reset` |
| "Save a checkpoint called before-upgrade." | `metacomputer stop`, then `metacomputer checkpoint before-upgrade` |
| "Go back to the before-upgrade checkpoint." | `metacomputer restore before-upgrade` |
| "Make the current state the new clean baseline." | `metacomputer baseline` |

- `reset` replaces the working VM with a fresh copy of the baseline. It copies the VM's task runs to the Mac first; recordings and results on the Mac are kept.
- Checkpoints are taken while the VM is stopped. `metacomputer checkpoints` lists them; `checkpoint NAME --replace` overwrites one; `checkpoint --delete NAME` deletes one.
- `baseline` keeps the previous baseline as `<BASELINE_NAME>-prev`.
- `reset`, `restore`, `baseline` and `checkpoint --delete` ask first (`--yes` skips the question). From a script or another program, with no terminal to ask on, they refuse unless given `--yes`. Claude never resets, restores, rebuilds or re-baselines the VM without your explicit OK.

### Add apps

The desktop user has no sudo, so system packages can't be installed from
inside the VM (a Python venv works for user-level packages). Add them to
`EXTRA_PACKAGES` in `config/local.env` instead. They come from Ubuntu's
archive, without "recommended" extras, and take effect on the next build.

| App | `EXTRA_PACKAGES` |
| --- | --- |
| Inkscape (vector drawing) | `inkscape` |
| Audacity (audio editing) | `audacity` |
| Kdenlive (video editing) | `kdenlive frei0r-plugins mediainfo` |
| pygame (games in Python) | `python3-pygame` |
| Ristretto (image viewer) | `ristretto` |
| GIMP (image editing; no verified rules yet) | `gimp` |

```bash
# config/local.env
EXTRA_PACKAGES="inkscape audacity"
```

```bash
metacomputer build --replace     # rebuilds the VM AND its baseline; everything in the VM is lost
```

Volumes, folder volumes and reference disks live outside the VM, so a rebuild
keeps them.

A rebuild gives the VM a new host key. If you use the secrets vault, run
`metacomputer secrets bind` again in your own terminal afterwards: until you
do, fills, sessions and tokens refuse the rebuilt VM, since it no longer
matches the key the vault pinned.

Then:
- "Draw a simple logo in Inkscape and export it as a PNG."
- "Trim the silence from the start of this recording in Audacity."

[EXTRA-APPS.md](../skills/metacomputer/EXTRA-APPS.md) has rules for Inkscape,
Audacity, Kdenlive and pygame windows, verified in a VM built this way. Some of their dialogs, canvases and timelines need real
pointer clicks instead of accessibility actions.

### Give the VM sound

`VIRTUAL_AUDIO=on` (next build) adds a sound card inside the VM that plays
into nothing: PulseAudio with a single null sink. Browsers, games and audio
or video editors get working sound output. Nothing reaches your Mac's
speakers or microphone: the virtio sound driver stays blacklisted, and Lume's
own audio sharing is the separate `SHARE_AUDIO` setting.

With the sound card, whole-desktop recordings include the VM's sound
(`RECORD_AUDIO=auto`, the default; `off` for video only). The sound is within
about 50 ms of the picture. `metacomputer status` shows the sound card when
the VM has one.

- "Record the whole desktop, with sound, while you edit this clip in Kdenlive." (with Kdenlive added through `EXTRA_PACKAGES`)

### Run a second VM side by side

`MCOMP_CONFIG` points the command at another settings file, used in place of
`config/local.env` (the defaults still apply). Give that VM its own
`VM_NAME`, `BASELINE_NAME`, `SSH_DIR`, `SSH_PORT`, `VNC_PORT`, `RUN_DIR` and
`LOG_DIR`, so the two never share a gateway, a lock or logs.
[config/local.env.example](../config/local.env.example) has a commented
example.

```bash
MCOMP_CONFIG=~/second.env metacomputer build
MCOMP_CONFIG=~/second.env metacomputer start
MCOMP_CONFIG=~/second.env metacomputer web compare https://example.com
```

The `metacomputer` MCP server that `install.sh` registers drives the VM named
in `config/local.env`. To give a Claude Code session the second VM instead,
start it with `MCOMP_CONFIG` set and an MCP configuration whose `metacomputer`
server passes the same variable. The benchmark runs its sessions this way.

```json
{"mcpServers": {"metacomputer": {"type": "stdio",
  "command": "/path/to/metacomputer/bin/metacomputer", "args": ["mcp-lazy"],
  "env": {"MCOMP_CONFIG": "/Users/you/second.env"}}}}
```

```bash
MCOMP_CONFIG=~/second.env claude --strict-mcp-config --mcp-config second-mcp.json
```

`--strict-mcp-config` leaves your other MCP servers out of that session.

### Turn on the optional hardening

- "Which hardening options are on for your computer?"
- "Show me exactly what you sent to the virtual computer in the last session."
- "Check that the action logs haven't been changed."

```bash
metacomputer security                 # what's on, in the config and in the VM, and the shell channel; --json
metacomputer security apply           # bring the running VM in line with the config (start does it too)
metacomputer actions list             # recent logged sessions; --days N
metacomputer actions show latest      # one session, readable; --json
metacomputer actions verify --all     # exit 1 if anything was changed
metacomputer actions verify FILE --key action-log.pub   # on another machine, with the public key
```

To turn one on, set it in `config/local.env` (for example `ACTION_LOG=on`,
`SSH_AUTH=cert`, or `DRIVER_MODE=bounded` with `DRIVER_PROFILE=browser` and
`DRIVER_ORIGINS="https://example.com"`), then run `metacomputer security
apply` or restart. Claude changes these settings only when you ask, never to
get around a limit. When bounded mode refuses a call, Claude tells you what
was refused and that you can allow it.

## Isolation and safety in detail

The [README](../README.md#isolation-and-safety) lists the protections that are always
on. This is the detail behind them; [SECURITY.md](../SECURITY.md) says where each one
ends, and [architecture.md](../architecture.md) has the design.

**Host sharing.** Lume attaches a SPICE clipboard port (your Mac's clipboard),
a virtio sound device (its speakers and microphone) and a Rosetta share to
every VM.
- With mcomp-vm, `metacomputer start` leaves out each one whose `SHARE_*` setting isn't `on`.
- With standard Lume all three are always attached. The VM has no `spice-vdagent` and `virtio_snd` is blacklisted, but a process with root in the VM could start an agent and read or set your clipboard. Don't keep secrets on the clipboard while it runs untrusted tasks.
- `metacomputer status` shows what the VM actually has, read from inside it (virtio console ports, a sound device, a Rosetta tag). With mcomp-vm and the defaults it reads `clipboard off  audio off  rosetta off`.

**Network.** The VM's network device talks to `cua-netgw`, a user-mode gateway
on your Mac built from Cua's source at a pinned commit. Every connection the
VM opens is checked on the Mac, not inside the VM:
- **Always refused:** your Mac's loopback and every address of its own interfaces (LAN and tailnet), the gateway itself, cloud metadata, multicast and broadcast.
- **Refused by [config/netgw-rules.json](../config/netgw-rules.json):** private LAN ranges (10/8, 172.16/12, 192.168/16), 100.64.0.0/10 (Tailscale and CGNAT), link-local, IPv6 ULA, benchmark and reserved ranges, and outbound SMTP (ports 25, 465, 587).
- **Allowed:** everything else, the public internet.
- DNS goes to the gateway, which resolves through 1.1.1.1 and 9.9.9.9, not your Mac's resolvers. Blocked attempts are logged to `logs/netgw-blocked.jsonl` (and, with an event sink, sent as `security.blocked_connections`).
- Inbound, there is no route to the VM except two forwards that listen on `127.0.0.1` only: SSH (port 2277 by default) and the live view (5977). The VM's firewall admits ports 22 and 5900 only from the gateway. There is no network bridge, so the VM never appears on your LAN.

**SSH and credentials** (all in `SSH_DIR`, default `~/.ssh/metacomputer`; the
private files are 0600):

| Credential | Grants |
| --- | --- |
| `mcp` | The desktop user `cua`, with a forced command: Cua Driver's MCP server only. No shell, no PTY, no forwarding |
| `ops` | A shell as `cua` (no sudo, no forwarding), for `metacomputer sh`, status, file copies and `inspect` |
| `admin` | The maintenance account `cuaadmin`, which after the build may use sudo only for `/usr/local/sbin/metacomputer-admin` and its fixed verbs |
| `fill` | The locked account `mcfill`, for `metacomputer secrets` fills: sshd forces its one root-owned program, with no TTY and no forwarding |
| `proxy` | The locked account `mcproxy`, for API tokens: no command and no TTY; it may open only one reverse forward, to `127.0.0.1:18765` inside the VM |
| `hostkey` | The VM's own SSH host key, generated on the Mac and pinned in `known_hosts`, so strict checking applies from the first connection |
| `vnc-pass` | The live-view password (or your own store, with `VNC_PASSWORD_STORE=command`) |

The VM's SSH accepts keys only, for `cua`, `cuaadmin`, `mcfill` and `mcproxy`, with
root login and every kind of forwarding off apart from `mcproxy`'s one reverse forward.
All accounts have locked passwords, and no macOS credentials are used anywhere.
After a successful build, the VM's private host key is deleted from the Mac.

On the Mac's side, every connection to the VM (`sh`, `ssh`, the driver,
recordings, status) runs ssh with no config file (`-F /dev/null`), so nothing
in your `~/.ssh/config` can add a forward or a proxy command, and with agent,
X11, port and tunnel forwarding and connection sharing turned off on the
command line. A hostile VM's sshd could ignore its own side's rules; the Mac's
side is what counts.

**The one way the VM can open a connection to your Mac** is the secrets API proxy,
and only while you use it. When the vault is unlocked and Claude asks for a token, the
vault opens that reverse forward, so the proxy appears at `127.0.0.1:18765` inside the
VM (`SECRETS_PROXY_PORT`). It accepts only valid tokens, sends requests only to each
secret's own API host, and stops when the vault locks.

**Your Mac.** Cua Driver isn't installed on it, and nothing in this project
can see or control its screen. The VM isn't started at login, and no
LaunchAgent or daemon is added. The event watcher, when you have a sink, is a
plain process that `start` and `stop` manage.

**Treat with care:**
- Everything the VM shows (pages, documents, window titles, accessibility text, shell output) is untrusted data, never instructions. The skill says so, but no model is immune to prompt injection; the protections above limit what a steered agent could reach.
- The VM can reach the public internet, so anything you put in it could be sent out. Don't copy secrets into it.
- Recordings and screenshots capture the whole screen.
- `ACTION_LOG_TEXT=keep` records typed text, scripts and shell output in full; keep such logs as safe as the passwords that might be in them.
- Event sinks' URLs, tokens and signing keys sit in a file anything running as you can read. Use ingest-only tokens.

## Known limitations

**Platform:**
- Apple-silicon Macs only, with an ARM64 guest.
- One controller per VM at a time (see [Start it and check on it](#start-it-and-check-on-it)). A second VM needs its own settings file.
- The VM can't reach your Mac or LAN, by design. Copy a local web app in as static files to test it.
- No root for the desktop user: system packages are added with `EXTRA_PACKAGES` and a rebuild, which also replaces the baseline.
- Unattended upgrades patch the working VM after each reset. Refresh the baseline now and then with `metacomputer baseline`, or rebuild, so resets start current.

**Cua Driver 0.53.0 on this desktop** (details in [DESKTOP.md](../skills/metacomputer/DESKTOP.md) and [BROWSER.md](../skills/metacomputer/BROWSER.md)):
- **No Firefox page route.** Page tools (`get_browser_state` and the `browser_*` tools) work only with Chrome, so page and hybrid modes need Chrome.
- **The focused element isn't reported** on Linux. Claude proves focus with a fresh read.
- **Firefox ignores background typing.** Text entry in Firefox uses foreground input, which moves the real pointer and focus.
- **Chrome ignores background input,** and while the driver's virtual input devices exist it ignores the keyboard altogether. The bridge sends input aimed at Chrome in the foreground, refuses `set_value` and `run_script` aimed at it, and removes leftover devices before Chrome needs the keyboard ([docs/chrome-investigation.md](chrome-investigation.md)).
- **Opening modal dialogs through accessibility actions can freeze GTK apps.** Mousepad's Save As through its menu, and Inkscape's dialogs, stopped answering; keyboard shortcuts work.
- **Some content isn't in the accessibility tree:** Thunar's icon view (use List View), Kdenlive's and Audacity's timelines, canvases and SDL games. These need pixel clicks; SDL window screenshots come back black.
- **A driver-launched Chrome closes** after 300 s without driver activity.
- **No compositor.** With compositing on, window shadows shift the driver's pixel mapping by about 20 px, so it stays off.

**The shell channel** ([docs/shell.md](shell.md)):
- Output comes back when the script ends, capped per stream; write big outputs to a file and `get` it.
- `--read-only` is a promise, not a sandbox: it skips the one-controller check and leaves out the desktop's environment, but doesn't stop a script from changing things.
- The sizes and hashes in a result are the VM's own report; a compromised VM could report anything that fits the schema.
- One controller at a time is checked and claimed in one step under the owners lock, for tool calls (in the hub, for every client including `web` and the GUI tests) and shell runs alike. It keeps sessions from clashing; it isn't a barrier against a program running as you, which can use the raw driver connection (`MCOMP_DIRECT_BRIDGE=1`) the transport tests use.
- `VM_SHELL=off` turns off this command, not SSH: `metacomputer ssh ops` still works for the Mac's own plumbing. Deny it in Claude Code's permissions if you want a barrier.

**Bounded mode** (`DRIVER_MODE=bounded`, [SECURITY.md](../SECURITY.md)):
- Cua runs page tools and desktop tools in separate runtimes, so one bounded session can't use both, and hybrid browsing needs standard mode.
- Manifests in driver 0.52.1 can't grant the clipboard tools, so they are always refused.
- `kill_app` refused to close apps the driver had launched; close them from their own window or with `metacomputer desktop-restart`.
- The `desktop` profile lists the standard apps only. Apps added with `EXTRA_PACKAGES` need your own `DRIVER_MANIFEST`.
- It limits the driver, not `metacomputer ssh ops` or programs already running in the VM; that's why it turns the shell channel off.

**Secrets** (`metacomputer secrets`; [SECURITY.md](../SECURITY.md#what-doesnt-hold) has the full list):
- Fills need page-mode Chrome and a VM built with the locked fill account; API tokens need the proxy account too. Older VMs say so; rebuild them. See [Use saved passwords and 2FA codes](#use-saved-passwords-and-2fa-codes).
- It isn't a wall against a deliberately hostile agent. With a shell in the VM (the default), code can pose as Chrome's DevTools to receive a fill, or read a filled field back. Driving the browser can reveal a field too: a "show password" button, Chrome's developer tools, a `javascript:` address.
- One user and one operator. Everything runs as your macOS user, so who asked, `NAME session` and `--project` are attribution that any program of yours can fake, and the person who unlocks also approves.
- No hardware module holds the data key. The Secure Enclave key takes part only in opening the vault; while it's unlocked, the unlock process holds the data key.
- The API key behind a token is the one you stored, not a credential minted for each use.

**Events** ([docs/events.md](events.md)):
- Best effort and at least once: a receiver that's down long enough can miss events, and a receiver should drop repeats by `id`.
- Against a program running as you, sink changes are detected and announced, not prevented.
- Rate limits hold back bursts (10 per family in 10 minutes; a phone also 30 an hour) and sum them up later.

**Recording:** the sound in whole-desktop recordings is within about 50 ms of
the picture. That's fine for watching; nudge it in an editor if a sound must
land on an exact frame.

[architecture.md](../architecture.md) lists the workarounds for Lume itself: its
VNC server isn't loopback-only (so it's disabled), `lume stop` is a hard
power-off (so `stop` shuts down from inside the VM first), and a gateway
serves one VM process (so `start` launches a fresh one).

## Troubleshooting

Problems during the install are in [install.md](install.md#troubleshooting).

| Symptom | What to do |
| --- | --- |
| `metacomputer status` shows a ✗ | Read that line. `metacomputer logs` shows the Lume, gateway and VM logs; `metacomputer restart` stops the VM and starts it again. |
| A Claude Code session has no metacomputer tools | Run `/mcp` and reconnect `plugin:metacomputer:metacomputer` (or `metacomputer` without the plugin), or restart Claude Code. `claude plugin list` should show `metacomputer@metacomputer` enabled (without the plugin, `claude mcp get metacomputer` shows `…/bin/metacomputer mcp-lazy`); `./install.sh --only-claude` sets it up again. |
| The plugin's MCP server stops with "no metacomputer set up yet" | The plugin came from GitHub, and this Mac has no checkout set up. Ask Claude to set up metacomputer, run the `setup` command the message prints, or run the one-line install ([install.md](install.md)). `MCOMP_HOME` can point at a checkout you already have. |
| The tools are listed twice (`mcp__metacomputer__*` and `mcp__plugin_metacomputer_metacomputer__*`) | Both the plugin and an older user-scope server are registered. `./install.sh --only-claude` removes the old one (or `--only-claude --no-plugin` removes the plugin). |
| "The virtual computer is off" | `metacomputer start`, then try again. |
| "Another session is using the virtual computer" | `metacomputer who` shows which. Ask before taking over, then `metacomputer release` (or `--override` on the command). |
| Chrome ignores typing | `metacomputer input-reset`. |
| An app stops answering (`atspi_app_unresponsive`) | It has to be closed (`kill_app`). `metacomputer desktop-restart` logs the desktop out and in, which closes every app. |
| A call is refused as "outside the capability manifest" | Bounded mode is on. Choose another `DRIVER_PROFILE`, or allow it in your own `DRIVER_MANIFEST`. |
| "Cua Driver's bounded session has ended" | Reconnect the MCP server with `/mcp` (`DRIVER_EXPIRES`, `DRIVER_IDLE`). |
| `metacomputer sh` exits 125 | Refused before anything ran: `VM_SHELL=off`, bounded mode with `VM_SHELL=auto`, another session using the VM, or an action log that couldn't be opened. The message says which. |
| `metacomputer sh` exits 255 | It couldn't run or finish: the VM is off, SSH failed, the `--cwd` folder doesn't exist, or the VM's answer was malformed. `--json` gives the `kind`. |
| `metacomputer sh` exits 124 | The script ran past `--timeout` (120 s by default) and was stopped. |
| `status` says the VM was built by an earlier version | Rebuild it: `metacomputer build --replace`. |
| `status` says REBOOT REQUIRED | Security updates are waiting for a reboot: `metacomputer restart`. |
| The installer stops | Fix what it names and run `./install.sh` again; finished steps are skipped. A failed build leaves its log in `logs/build-*.log`. If provisioning failed inside the VM, it is left running so you can look: `metacomputer ssh admin`. |
| mcomp-vm won't build | It needs Apple's Command Line Tools (`xcode-select --install`). Or use `./install.sh --lume=standard`. |
| `metacomputer: command not found` | Add `~/.local/bin` to your `PATH`, or use the full path. |
| `record start` refuses `RECORD_DIR` | Its parent folder doesn't exist, usually a drive that isn't connected. |
| `metacomputer secrets` says the vault is locked | Run `metacomputer secrets unlock` in your own terminal, and keep it open. |
| `secrets set` says a secret with that name exists | Add `--replace` to rotate its value, or delete it first. |
| Fills and tokens are refused because a required audit sink can't be reached | `metacomputer secrets audit-sink list` shows its last error. Fix the receiver, or remove the sink in your own terminal. |
| The unlock terminal says `AUDIT LOG CHANGED` | A record in `audit.jsonl` failed the vault's checks. `metacomputer secrets audit --verify` names it; the sinks got an `audit_tamper` notice in its place. |
| A program in the VM gets 401 from the proxy | The token expired, was revoked, or its secret changed: ask for a new one. |
| `secrets token` or `fill` says to rebuild the VM | The VM predates the proxy or fill account: `metacomputer build --replace`, then `metacomputer secrets bind` again. |
| `notify` says nobody was pinged | No event sink is set up. Add one in your own terminal (`metacomputer events sink add`). |
| Events aren't arriving | `metacomputer events status` shows failing sinks and backlogs; `events sink test NAME` tries one now. The watcher retries backlogs every 30 s once their backoff has passed. |
| `events sink add` refuses | It needs your own terminal (not an agent's shell, a script or a pipe). |

