# Using metacomputer: what to ask Claude Code

This guide goes through what the virtual computer can do, one task at a time:
what to ask Claude Code, and the command behind it if you'd rather run it
yourself. The [README](../README.md) has the overview,
[install.md](install.md) the install, and [commands.md](commands.md) every
command and setting.

- [Prompt a computer into existence](#prompt-a-computer-into-existence)
- [How-to guide](#how-to-guide)
- [Isolation and safety in detail](#isolation-and-safety-in-detail)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## Prompt a computer into existence

Each of these is something you can type into Claude Code with the plugin
installed. Claude decides what runs on the desktop and what goes through the
shell, says which computer it's using, and ends with a check and the paths of what
it made. Where a prompt needs an app that isn't in the standard VM, it says which
package to add to `EXTRA_PACKAGES` (and the VM then needs a rebuild; see
[Add apps](#add-apps)).

**Creative**

| You say | What happens |
| --- | --- |
| "Draw a flat two-colour logo for a coffee shop called North Star in Inkscape, export a 512 px PNG, and give me the SVG and the PNG." | Claude draws on the canvas with real pointer clicks, opens dialogs with their shortcuts, saves early, and copies both files to your Mac. Needs `inkscape`. |
| "Trim the silence from these nine sound effects in Audacity, fade each one out, level them, and export them as WAV." | Claude selects on the waveform by pixel, works the menus from the keyboard, and exports each file with a typed path. It can't hear, so it judges the sound from the waveform. Needs `audacity`. |
| "Put these three clips on a timeline in Kdenlive, in order, and save the project." | Claude adds the clips and arranges them by pixel, since the timeline isn't in the accessibility tree, then saves with Save As. Needs `kdenlive frei0r-plugins mediainfo`. |
| "Resize these 40 photos to 1600 px wide and make me a contact sheet of all of them." | A script with ImageMagick in the VM does the resizing and the sheet; you get a zip back. |
| "Record yourself editing this clip in Kdenlive, with the sound." | A whole-desktop recording with the VM's own sound card. Needs `kdenlive frei0r-plugins mediainfo` and `VIRTUAL_AUDIO=on`. |

**Office and data**

| You say | What happens |
| --- | --- |
| "Here's last quarter's sales CSV. Build a Calc workbook with totals by region and a column chart, and save it as .ods and .xlsx." | Claude works in LibreOffice Calc on the desktop, using pixel clicks for the chart wizard, and checks the saved files. |
| "Convert every .docx in this folder to PDF and give me a zip." | One `metacomputer sh` script: LibreOffice headless with its own profile, then `zip`. |
| "Pull the date, invoice number and total out of these 30 PDF invoices into one spreadsheet." | `pdftotext` and Python in the VM, written out with openpyxl, then a look at the result in Calc. |
| "Fill in this Word template once for each row of the CSV, one document per customer." | python-docx in the VM fills the template; Claude opens one result in Writer to check the layout. |
| "Load these CSVs into SQLite and tell me which customer ordered the most in March." | sqlite3 in the VM, with the query and its answer in the reply. |
| "Fix the broken formulas in column F of this spreadsheet, and show me what you changed." | Calc on the desktop, because the open document is what gets saved; Claude lists the cells it changed. |
| "Write my notes up as a memo in Writer: a Heading 1 title, a bulleted list, page numbers. Save it as .odt and .docx." | Writer on the desktop, styles from its menus, and both files read back to check. |

**Sign-ins and keys**

| You say | What happens |
| --- | --- |
| "Log me into github.com on the virtual computer with my saved login, then open my notifications." | Claude opens the page in page-mode Chrome and runs `metacomputer secrets login`; the vault types the username and password, under one approval when the login asks (imported logins do), confirmed with Touch ID on a Mac that has it. Claude gets back the outcome, never the password. |
| "Sign in to the demo dashboard with my `demo` login and its 2FA code from the vault, then download this month's report." | The vault types the username, password and current code; Claude never sees them. You approve in your terminal if you asked to. |
| "Bring my Chrome passwords into the vault." | Claude can't do this one for you: it gives you `metacomputer secrets import-passwords --browser chrome` for your own terminal. macOS asks whether the vault may use Chrome's key, and imported logins ask before every use. |
| "Use my GitHub session from my own Chrome on the virtual computer, so it doesn't have to sign in again." | You bring that one site's cookies in from your terminal (`import-session`); Claude loads them into a VM profile, with your approval. Cookies only. |
| "Let this project use the staging login without asking me for the next 8 hours." | Claude gives you the `metacomputer secrets rule add ... --for 8h` command; making the rule takes Touch ID or the passphrase. Google, Microsoft, Apple and Okta sign-ins still ask every time. |
| "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there." | Claude gets a 15-minute token; the script calls the proxy inside the VM, and the proxy on your Mac adds the real key. |
| "Give me Claude inside the metacomputer, signed in with my subscription, and show it on the desktop." | `metacomputer claude --token claude-sub --show`: Claude Code installed in the VM and started in a terminal you can watch, signed in through the vault's proxy with a 1-hour token; your subscription token stays on the Mac. Claude talks to it with `claude send` and `claude read`. |
| "Have the Claude in the VM try this stranger's repo with permissions skipped, and tell me what it does." | `--skip-permissions` inside the disposable VM: the inner Claude works without asking, the VM's isolation is the limit, and the outer Claude reports what it saw. `claude stop --revoke` ends the token; `reset` (with your OK) cleans the VM. |
| "Keep me signed in to the admin site between runs." | Claude seals the signed-in Chrome profile into the vault and loads it next time; each load asks you, since a whole profile can hold any sign-in. (Sealed sessions need `SSH_AUTH=key`, the default, for now.) |

**Files that outlive the computer**

| You say | What happens |
| --- | --- |
| "Keep my project files on a volume so they survive a reset." | `metacomputer volume create work --size 20G` and `VOLUMES=work`: a disk image on your Mac, attached at the next start, that outlives resets and rebuilds. |
| "Put your results in a folder I can open in Finder." | A folder volume, read-write because you asked. The folder's name needs `.noindex`, and a watcher on your Mac quarantines everything the VM writes there. |
| "Give the VM a read-only copy of this folder of reference papers." | `metacomputer refdisk create`: a read-only disk image that any number of VMs can attach. |
| "Use the project files in our team bucket, and put your results there." | An S3 volume: rclone on your Mac keeps a folder in step with the bucket. You type the keys into the Keychain yourself (`volume keys`); they never enter the VM. |
| "Checkpoint the computer and its work volume before you try the migration." | `metacomputer checkpoint NAME --with-volumes` while the VM is stopped (APFS clones where it can); `restore` rolls both back, after asking you. |

**Web**

| You say | What happens |
| --- | --- |
| "Does our landing page look the same in Firefox and Chrome? Show me the differences." | `metacomputer web compare`: both browsers side by side, with differences in headings, links, controls and text. |
| "Show me the pricing page at desktop, tablet and phone widths, and tell me where the layout breaks." | `metacomputer web responsive` at 1920, 1280, 768 and 500 px, then a look at each screenshot. |
| "Fill in the contact form on our public staging site with test data and confirm the thank-you page appears." | Chrome in page mode fills each field exactly, submits, and checks the next page. |
| "Run an accessibility check on our sign-up page." | `metacomputer web a11y`: unlabelled controls, images without text alternatives, missing headings, vague links. |

**Testing and QA**

| You say | What happens |
| --- | --- |
| "Build the site, serve it inside the VM, and click through sign-up, checkout and the account page in both browsers. Tell me what breaks." | Claude copies the build in, serves it on the VM's own localhost, and works each flow like a user. |
| "Record a repro of the bug where Save stays disabled after editing the title, with a contact sheet I can attach to the issue." | A task folder, a recording with per-action traces, and a contact sheet copied to your Mac. |
| "Compare today's pages with last week's run and list the ones that changed." | `metacomputer web responsive --baseline` with similarity scores and difference images. |
| "Run the GUI smoke tests and tell me if anything fails." | `metacomputer test`: six recorded tests, each checked from the app's state and the files on disk. |
| "Check every link in the footer and tell me which ones fail." | A script in the VM, then a browser look at the ones that did. |
| "Upload this CSV through the import page in Chrome and check that the row count matches." | Page mode handles the file picker; Claude reads the count back from the page. |

**Safety**

| You say | What happens |
| --- | --- |
| "Someone sent me this link. What does it do, and is it safe to open?" | `metacomputer web open-untrusted`: redirects, the final page, any password prompts or downloads, with nothing on the page clicked. |
| "This PDF came from an unknown sender. Open it on the virtual computer, not my Mac, and tell me what's in it." | The file goes into the VM only; `pdftotext` and `pdfinfo` read it there, and Claude reports what it says and any web addresses in it. |
| "Download the installer from this site in the VM and tell me its checksum and what's in the archive, without running it." | `curl`, `sha256sum` and `unzip -l` in the VM. Nothing reaches your Mac. |
| "Where do these three shortened links really go?" | One `open-untrusted` run per link, each in a throwaway profile. |
| "Did the VM try to reach anything on my network today?" | Claude reads the gateway's log of blocked connections on your Mac and sums it up. |
| "What did you do on the virtual computer in the last session?" | With `ACTION_LOG=on`, `metacomputer actions show latest` lists every tool call and shell run, and `actions verify` checks nothing was changed. |

**Getting better at the job**

| You say | What happens |
| --- | --- |
| "What do you keep getting wrong on the virtual computer? Show me the evidence." | `metacomputer learn report`: failed calls by class, repeated mistakes and time per app from your own sessions, with proposed lessons and their counts. |
| "Approve the no-frame lesson." | Claude approves only the IDs you name. The lesson reaches later sessions as advice that can't switch off a guard; lessons Claude drafted need your own terminal. |
| "Do my lessons actually help? Measure it on the test computer." | `metacomputer learn bench`: the benchmark with and without them, on a test VM. It says how many sessions it will run and asks first, since it spends your Claude usage. |

**Games**

| You say | What happens |
| --- | --- |
| "Write a small arcade game in Python with pygame, run it on the virtual computer and play a round to check the controls." | Claude writes the game, starts it on the desktop through the shell, and plays from desktop screenshots and key presses. Needs `python3-pygame`. |
| "Give the game an autopilot that plays it, run 100 autopilot games, and put the scores per level in a Calc sheet with a chart." | The games run headless through `metacomputer sh`; the numbers go into a Calc workbook with a chart. Needs `python3-pygame`. |
| "Tune the difficulty table until a beginner autopilot clears level 1 at least 80% of the time. Show me the numbers before and after." | Claude changes the table, reruns the autopilot, and repeats until the goal holds. |
| "Playtest a level and write down every bug and confusing moment you hit." | Claude plays from desktop screenshots, since a game window's contents aren't in the accessibility tree, and keeps notes as it goes. |
| "Draw the game's ships in Inkscape, make its sound effects in Audacity, and cut a short trailer in Kdenlive." | Each in its own app, with real pointer clicks where a canvas, waveform or timeline needs them; the README's game-studio example walks through it. Needs `inkscape audacity kdenlive frei0r-plugins mediainfo`, and `VIRTUAL_AUDIO=on` for sound. |

**More than one computer**

| You say | What happens |
| --- | --- |
| "Set up a second virtual computer called `staging`, Firefox only, beside the main one." | `metacomputer vm create staging` writes its settings (its own names, ports, keys and vault) and builds it with your OK; Claude sets `BROWSERS="firefox"` in `config/vms/staging.env` first. About 5 GB more disk to start, growing as it's used. |
| "Keep the main computer signed in to the CMS, and use the second one, with no logins at all, to check what a visitor sees." | Two VMs with separate browsers, profiles and vaults; the browser checks run against the second with `metacomputer --vm staging web ...`. |
| "Which virtual computers do I have, and how much disk do they use?" | `metacomputer vm list`: each VM's state, disk use, baseline, ports and settings file. |
| "Delete the `staging` computer." | `metacomputer vm delete staging` lists what goes (the VM, its baseline, its settings) and what stays (volumes, its vault folder), then waits for you to type the name. |
| "Run the benchmark on the test computer while I keep working on the main one." | `bench/bench.py` resets and drives only the VM named in its `MCOMP_CONFIG`. |

**Recordings and demos**

| You say | What happens |
| --- | --- |
| "Record a demo of signing up for our app in Chrome, with the typing visible." | Hybrid mode: real clicks and key-by-key typing for the video, page reads to check each step. |
| "Record the whole desktop while you build a budget in Calc, and give me the key moments with timestamps." | A whole-desktop recording with a description file; with `ACTION_LOG=on` it lists every tool call and its time in the video. |
| "Make a contact sheet of yesterday's recording so I can see what happened without watching it." | `metacomputer contact-sheet`: 12 frames in one image. |
| "Record at editing quality and stream it straight to my external drive." | Claude sets `RECORD_QUALITY=high` and `RECORD_DIR` because you asked, then records (streaming to the Mac is the default). |
| "Run this script from a repo I don't trust, and record everything in case it wrecks the VM." | A whole-desktop recording streamed to your Mac as it's made, so a script that wipes the VM can't take it back; then `metacomputer reset`. |

**Research and automation**

| You say | What happens |
| --- | --- |
| "Visit these ten competitors' pricing pages and put plan names and prices in a spreadsheet, with a screenshot of each page." | Firefox for the pages, Calc for the table, one screenshot each in the task folder. Page text is treated as data. |
| "Download the March statements from the supplier portal, rename them by date, and zip them." | Sign-in from the vault (or by you, through the live view), downloads in Chrome, renaming and zipping through the shell. |
| "Work through these 20 forms. Ping my phone if you hit a CAPTCHA or a sign-in, and when you're done." | With an ntfy sink set up, a CAPTCHA or sign-in pauses Claude with `metacomputer handoff`, which reaches your phone; you do that step in the live view and run `metacomputer handoff done`. The finished task is announced too. |
| "Send my metacomputer events to our Slack." | Claude gives you the exact command for your own terminal, and the small relay from docs/events.md. Sinks only change from your terminal. |

**Teach it a new app**

| You say | What happens |
| --- | --- |
| "Add GIMP to the computer, then find out which of its menus and dialogs work through accessibility and which need real clicks. Write the rules down like DESKTOP.md." | After the rebuild (which needs your OK), Claude explores and writes notes you can add to the skill. GIMP has no verified rules yet. Needs `gimp`. |
| "Learn our web app's admin screens and write a short guide, with screenshots, that the next session can follow." | Claude maps each screen from its accessibility tree and saves a Markdown guide with screenshots. |
| "Try Inkscape's export with each method you have and tell me which works best here." | Claude compares the shortcut, the menu and the command line, and reports which hung and which worked. Needs `inkscape`. |

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
itself. It opens the window and hands the computer to you to sign in
(`metacomputer handoff --why login`; see
[When Claude needs you](#when-claude-needs-you-handoffs-and-the-live-view)), or
uses the vault (next section). Later sessions with the same profile name reuse
its cookies until the VM is reset.

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
- **Approving each use:** a secret you add with `set` goes ahead once the vault is unlocked (`--approval session`, the default), while imported logins and saved browser logins ask every time until you relax them. Add `--approval each` to `set` (or `policy NAME --approval each` later, or `--confirm each` to `unlock`), and every use waits for you in that terminal. It shows the secret, the site and who asked (for example `Asked by Claude Code in ~/Projects/foo`). What you type decides how far the answer reaches:
  - `NAME`: this use only;
  - `NAME session`: this use and later uses of that secret from the same Claude Code session;
  - `NAME unlock`: every use of that secret until the vault locks.
- **Approving from elsewhere:** each waiting use has a request id. `metacomputer secrets requests` lists them (who asked, the site, a masked username, how long it has waited); `metacomputer secrets approve ID` allows one with Touch ID or your login password, `approve ID --passphrase` with the vault passphrase, and `deny ID` refuses it. `unlock --touch-id` shows the Touch ID dialog for every question by itself. In presence mode (below), a program can't approve by typing into the unlock terminal: only your finger, your password or your passphrase does. With typed approvals, a program that can type into the unlock terminal could answer for you ([SECURITY.md](../SECURITY.md#what-doesnt-hold), "Keystroke injection into the unlock terminal").
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
- Unlock in a plain terminal window. Inside tmux or screen, another program could answer for you, so uses that need your answer are refused there unless you unlock with `--touch-id` (answer with Touch ID instead) or `--allow-multiplexer`. That detection is a list: an editor's built-in terminal or a terminal scripted through AppleScript isn't caught, and there too a program could type the answer.

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
metacomputer secrets token renew ID           # keep it going: the same token in the VM, so nothing there restarts (--all for every one)
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
- `token renew` moves a live token's expiry later under the same rules as a new token (an `each` secret asks you again), never past the unlock and never more than 12 hours after the token was made. A long job in the VM, Claude Code included, keeps working on the same token. The Claude Code session a token was made for, and you at your terminal, renew it under the secret's own policy; anyone else (another session, a script) is asked about at your unlock terminal, even for a `session` secret.
- One answer may take 10 minutes; `--answer-timeout 30m` on `set` (or `policy NAME --answer-timeout 30m` later) allows longer, up to an hour. Whatever the limit, the API must send its first byte, and each next one, within 120 seconds. An answer still streaming when its token ends (revoked, expired, its secret replaced or deleted, the vault locking) is cut off.
- A token is a bearer credential: whatever holds it can call that API as you until it ends. It can't get the key or reach another host.
- The key is withheld only as an exact copy; an API that sent it back transformed (in base64, say) would hand it over.
- The VM needs the proxy account, which VMs built before API tokens don't have. `token` says so; rebuild the VM.

### Claude inside the metacomputer

- "Give me Claude inside the metacomputer, and show it on the desktop."
- "Ask the Claude in the VM to build this in its own folder, and tell me when it's done."
- "Let the Claude in the VM run with permissions skipped and try this repo; then stop it and revoke its token."

`metacomputer claude` runs Claude Code inside the VM, signed in through the vault's API
proxy, so your subscription token or API key stays on the Mac. It's the API-key setup
above, done for Claude Code.

**Once, in your own terminal:** store the credential as an api secret for
`api.anthropic.com`.

```bash
claude setup-token                      # on the Mac: prints a long-lived subscription token
metacomputer secrets set claude-sub --kind api --api-host api.anthropic.com           # paste it here
# or an API key, which Claude Code sends as x-api-key:
metacomputer secrets set claude-key --kind api --api-host api.anthropic.com --header 'x-api-key: {value}'
metacomputer secrets bind                # if this VM isn't bound yet
metacomputer secrets unlock --for 2h     # keep this terminal open
```

**Then Claude (or you) runs:**

```bash
metacomputer claude --token claude-sub --show      # install if needed, a 1-hour token, start it in tmux
metacomputer claude send "Write a CSV parser in ~/claude-work/parse.py, with tests" --wait
metacomputer claude read --lines 80                # its screen and history, escaped
metacomputer claude keys 1                         # answer a question it asks (or Enter, Escape, BTab, y, n)
metacomputer claude status                         # running? the token's time left
metacomputer claude renew --ttl 1h                 # the same token lives longer; Claude Code keeps running
metacomputer claude stop --revoke                  # end it and its token
```

- **Start** installs Claude Code with Anthropic's native installer if the VM doesn't have
  it (about 10 seconds, into the desktop user's `~/.local/bin`, which it adds to that
  user's PATH). It then asks the vault for a token: approval as usual, 1 hour by default
  (`--ttl`, at most 8 hours, never past the unlock).
  - It sets Claude Code's first-run choices in the VM's `~/.claude.json`: onboarding done,
    the working folder trusted, and an API key approved. Claude Code starts ready to use.
  - It runs in a tmux session, in `~/claude-work` (`--cwd DIR`). `--model M`,
    `--skip-permissions`, `--session NAME` (several at once) and `-- ARGS` (Claude Code's
    own, such as `-- --continue`) are there when you need them.
- **The header decides how Claude Code is signed in.** With `Authorization: Bearer` (the
  default, for a subscription token) the token goes in `CLAUDE_CODE_OAUTH_TOKEN`; with
  `x-api-key` it goes in `ANTHROPIC_API_KEY`. Either way `ANTHROPIC_BASE_URL` points at the
  proxy inside the VM, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` keeps telemetry and
  update checks off.
- **Talking to it goes through tmux, not the screen.** `send` pastes the prompt (so it
  arrives whole, newlines included) and presses Enter; `--wait` returns once the screen
  has held still for a few seconds, with the screen. `read` captures the screen and its
  history. Keystrokes sent to its window through the desktop get lost, so Claude doesn't
  use them.
- **Watching it:** `--show` (or `claude show` later) opens a terminal on the VM's desktop
  attached to the same session, so it's in the live view (`metacomputer view`) and in
  recordings. You can type into it yourself there.
- **Keeping it going.** When the token ends (its time is up, or you lock the vault),
  Claude Code can't reach the API, and an answer it's in the middle of stops.
  `metacomputer claude renew [--ttl 1h]` moves the same token's expiry later without a
  restart. It goes through the vault like a new token, so an `each` secret asks you again.
  `send`, `wait` and `status` warn when fewer than 10 minutes are left. If a renewal is
  refused, or the token has already ended, `claude stop` and start again with
  `-- --continue` to pick up the conversation.
- It runs through the shell channel, so `VM_SHELL=off` and bounded mode turn it off, and
  starting it, sending to it and stopping it count as using the computer (one session at a
  time). While the inner Claude works, a keeper on the Mac marks the computer as in use
  for the session that started it, so other sessions are refused; `metacomputer who` shows
  it, and `claude stop` (or the inner Claude's exit) releases it.
- The hub's guards and the action log cover what goes through metacomputer: starting it,
  what it's told, what's read back. What the inner Claude then does in the VM, through its
  own tools, isn't seen by them; the VM's isolation is its limit.
- `claude stop --revoke` revokes the token even if the VM can't be reached, and if
  anything is left undone it says so, keeps the session's record and exits non-zero:
  run it again.
- What the inner Claude can and can't reach, and where the token goes:
  [SECURITY.md](../SECURITY.md#claude-inside-the-vm-metacomputer-claude). In short, it can do
  what the VM's desktop user can do, its token works only through the proxy until it ends,
  and anything in the VM can read that token while it lives.

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
metacomputer secrets audit-sink add otel --kind otlp --url http://127.0.0.1:4318 --required   # uses wait for it to accept their record
metacomputer secrets audit-sink add siem --kind syslog --url syslog+tls://siem.example:6514     # best effort: syslog can't acknowledge a record
metacomputer secrets audit-sink add phone --kind ntfy --url ask     # the topic is typed, not echoed
```

- `--required` (webhook and OTLP sinks only; syslog and ntfy can't acknowledge a record) fails closed: each use is first recorded and must reach every required sink before it goes ahead. While one can't be reached, fills, tokens and session saves and loads are refused, and proxied API requests get 503.
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
- **Per task, through Cua Driver.** Claude makes a task folder and calls `start_recording`. It produces a 1920×1080, 30 fps MP4 and a folder per action with the state, screenshots and click markers before and after, written in the VM. While the task folder is open, the Mac copies each finished action's folder every 15 s (`TASK_SYNC_INTERVAL`), once; `metacomputer task finish` copies the rest, the video included, never replaces what was copied earlier, and makes a contact sheet.
- **The whole desktop, whoever drives it.** `metacomputer record start NAME` and `record stop` capture the screen with ffmpeg, at 20 fps by default, streamed to the Mac as it records. A description file (`NAME.json`) beside the MP4 lists its times, settings, length, size, sound track and SHA-256. With `ACTION_LOG=on` it also lists the key moments: every tool call and shell run made on this VM while it recorded, with its time in the video (for example `0:42 launch_app inkscape`). A call that didn't run says so, as the action log records it: `(refused)` when a guard refused it, `(error)` when it came back as an error, Cua Driver's own refusals in bounded mode included.

```bash
metacomputer record start demo          # name and, optionally, frames per second
metacomputer record stop                # saves recordings/manual/demo.mp4 and demo.json
metacomputer record moments recordings/manual/demo.mp4
metacomputer recordings                 # recent recordings; --sync copies the VM's task runs to the Mac
metacomputer contact-sheet FILE.mp4     # 12 frames in one image
```

`RECORD_QUALITY=high` is sharp enough to edit into videos (several times
larger). `RECORD_DIR` can be any folder, such as an external drive. Shell
steps don't show in a recording, so for a recorded demo Claude uses the apps.

**When the task might turn on the VM.** Recording exists to capture runs like a
script from a stranger's repository, so a desktop recording goes to your Mac as
it's made (`RECORD_TO=host`, the default): if the script wipes the VM's home
folder, what was recorded up to the end is on the Mac already, and `record stop`
saves it. The VM's SSH keys aren't in the home folder, so the wipe doesn't cut your
Mac off: `metacomputer sh` and `task finish` still work afterwards. If the VM stops
sending before you stop it, `record stop` says so and the description file
records it. `RECORD_TO=guest` records inside the VM instead and copies at
`record stop`; anything running there can delete it first, and `record start`
warns. For a per-task recording, the newest step and the video are only copied
at `task finish`, so for an untrusted run Claude also records the whole desktop.
[SECURITY.md](../SECURITY.md#recordings) has the details.

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

- **On the Mac:** `metacomputer view --open` (or just `metacomputer view` in
  Terminal on the Mac's screen). Screen Sharing opens the live view. The first
  time, it asks whether Screen Sharing should remember the live-view password:
  say yes and it's saved in your login keychain, where Screen Sharing finds it,
  so from then on the live view opens without asking for a password (each
  `view --open` keeps it up to date after a rebuild; `view --forget` removes it;
  `VIEW_REMEMBER=off` never saves it, or updates one saved before; `--remember`
  and `--forget` run only in your own terminal). Say no, or run it where nobody can answer,
  and the live-view password is on your clipboard instead: paste it with Cmd+V
  when Screen Sharing asks for a password (it isn't your Mac's password). It's
  cleared from the clipboard after 45 seconds, if nothing else was copied since
  (`VIEW_CLIPBOARD_S` changes that). It's never put in the `vnc://` link,
  on a command line, in a log or in an event.
- **From another device:** `metacomputer view` (over SSH) or `view --print`
  prints the SSH tunnel (`ssh -N -L 5977:127.0.0.1:5977 <your-mac>`); then
  connect a VNC client to `localhost:5977`. Run `view --open` over SSH and it
  says this instead, since that terminal isn't on the Mac's screen.
- **Over your own tailnet:** `metacomputer view --tailnet` prints the
  `tailscale serve` command that publishes the port to your tailnet, and its
  undo. metacomputer never runs it: it changes your Mac's networking, so it's
  your choice ([SECURITY.md](../SECURITY.md#the-live-view-metacomputer-view)).

The password comes from where your setup keeps it: the login Keychain (item
`metacomputer-vnc`) on an install made with get.sh or `./install.sh`,
`~/.ssh/metacomputer/vnc-pass` on an older one, or your own secret store (see
`VNC_PASSWORD_STORE` under [Configuration](commands.md#configuration)).
`metacomputer view --print` shows the command that reads it, for your own terminal.
On the clipboard it's marked concealed and transient, which clipboard managers
that follow those markers don't keep in their history (one that ignores them
may), and with Universal Clipboard your other Apple devices nearby can paste it
too. The copy Screen Sharing remembers is an item in your login keychain that
Screen Sharing and osascript can read without asking; how it's written, and
where that ends, is in [SECURITY.md](../SECURITY.md#the-live-view-metacomputer-view).

### When Claude needs you: handoffs and the live view

- "If you hit a CAPTCHA, hand it to me and wait."
- "Let me sign in to the supplier portal myself."

Some steps are yours: a CAPTCHA or "Verify you are human" check, a sign-in you'd
rather do yourself, a 2FA prompt the vault can't fill. Claude never tries to get
past them. It hands the computer to you and waits:

1. It runs `metacomputer handoff --why captcha --say "Tick the 'Verify you are human' box on example.com"`.
   That tells you through your event sinks (your phone, with an ntfy sink), and
   Claude says the same in the chat.
2. You run `metacomputer view --open` (Screen Sharing connects with the password
   it remembers; if you didn't let it, paste the password with Cmd+V), and do the
   step in the live view. With `INTERACTIVE_PROMPTS=on` in your settings, a dialog
   on the Mac's screen also says what Claude needs, and its **Open live view**
   button does the same (it's off by default, so nothing pops up while you're away).
3. You run `metacomputer handoff done` in your terminal (add
   `--note "use the second account"` for anything Claude should know), or
   `metacomputer handoff cancel` to call it off.
4. Claude reads the screen again and carries on, or, after a cancel, stops and
   asks you how to go on.

While a handoff is open, Claude's actions on the desktop wait: the hub refuses
every acting call, from every session and script, with "Paused: the user has
the computer", and so do `metacomputer sh` (but `--read-only`), `metacomputer
claude`'s `start`, `send`, `keys`, `show` and `stop` (an inner Claude that's
working carries on with what it was given; its keeper stops renewing its hold on
the computer), fills and
session loads from the vault, `input-reset`, and `stop`, `reset` and the other
commands that check for other controllers (`--override` still goes ahead on
those, as it does for another session). The hub's own cleanup waits too, and it
keeps its connection to the VM (no idle exit, no restart), so the browser you're
working in stays; `metacomputer web` leaves its Firefox windows open, and the GUI
tests stop. Claude can still read the screen. `metacomputer who` and
`metacomputer handoff status` show it. It's a cooperative pause with stated
limits: something that had already left your Mac when it opened (a call, a shell
run) finishes ([SECURITY.md](../SECURITY.md#handing-the-computer-to-you-metacomputer-handoff)).

Opening a handoff waits for anything already on its way to the VM: a call being
sent, a shell run starting, or a fill from the vault, which counts from its start
until it ends, approval included. After 40 seconds it gives up and says why;
Claude then waits for that to finish and hands over again. If a fill ended
without the vault confirming that it stopped, the handoff first runs
`metacomputer secrets settle`, and refuses until that confirms.

It ends when you run `handoff done` or `handoff cancel`, or at its time limit
(15 minutes unless Claude chose another, at most 2 hours); then the computer is
Claude's again, and it asks you before trying anything else. If you ran
`metacomputer handoff` yourself at a terminal, Enter there is "done", and `c`
then Enter, or Ctrl-C, is "cancel". The Claude Code session that asked can't
say "done" for you from its own shell; it can only call its handoff off.

```bash
metacomputer handoff status            # the open handoff, what to do, how long it has left
metacomputer handoff done [--note T]   # you: finished, Claude carries on
metacomputer handoff cancel            # you: called off, Claude stops and asks
metacomputer view --open               # Screen Sharing: the password it remembers, or on the clipboard
```

**Sites with bot checks.** Some sites refuse the VM's browser even when you tick
the box yourself. Page-mode and hybrid Chrome are driven through Chrome's
DevTools, and bot checks (Cloudflare Turnstile among them) look for that, so the
check can keep failing or loop. metacomputer doesn't disguise its browser and
Claude won't try to get around a check (no other browser, mode or profile, no
retrying in a loop). If it keeps failing for you, do that step on your own
computer, or ask Claude to skip that site.

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

- When Claude is blocked on you (a dialog it won't accept, an approval) it runs `metacomputer notify "what it needs" --why approval --action "what to do"` and says the same in the chat. For a CAPTCHA or bot check, a sign-in you do yourself or a 2FA prompt it runs `metacomputer handoff` ([above](#when-claude-needs-you-handoffs-and-the-live-view)), which notifies you the same way and pauses Claude until you're done.
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

One command makes it. `vm create` writes `config/vms/staging.env` with the VM's own
names, ports (the first free pair from 2300 unless you give `--ssh-port` and
`--vnc-port`), SSH keys folder, vault folder and event spool, copies this Mac's
choices (the runner, `BROWSERS`, memory, sharing and hardening settings) from
`config/local.env`, and builds it after asking:

```bash
metacomputer vm create staging
metacomputer --vm staging start
metacomputer --vm staging web compare https://example.com
```

`--vm NAME` goes before the command; `export MCOMP_VM=staging` does the same for a
whole shell. Everything follows the VM: its gateway, hub, locks, logs, live view,
volumes, and its vault, so `metacomputer --vm staging secrets init` (then `bind`,
`set`, `unlock`, in your own terminal) makes a vault for it alone, and its events
come through its own spool. Commands that change a VM, and every `secrets`
command, start with one line saying which VM (and vault) they act on:

```
metacomputer: VM staging (config/vms/staging.env), vault ~/Library/Application Support/metacomputer-vms/staging
```

Edit `config/vms/staging.env` like `config/local.env` (it's read on its own, after
the defaults; `config/local.env` isn't read for it). Its folders are fixed by its
name, beside the main VM's (`.run-vms/staging`, `logs-vms/staging`,
`~/.ssh/metacomputer-staging`, `.../metacomputer-vms/staging`), and can't be set in the
file; if one of them is a link into another VM's folder or into the runner's VM storage
(`~/.lume`), the file is refused. `metacomputer vm list` shows
every VM with its state, disk use, ports and baseline, and the runner's other VMs.
[commands.md](commands.md#more-than-one-vm) has the whole scheme.

When it isn't clear which VM is meant, commands refuse instead of guessing: a
checkout with no `config/local.env` of its own (a second clone, a worktree) acts on
a VM only when you say which with `--vm`, unless the runner has no VM but the
default one. `MCOMP_CONFIG=FILE` still works as before, for settings files kept
elsewhere.

Deleting one:

```bash
metacomputer vm delete staging                  # says what goes and what stays, then you type the name
metacomputer vm delete staging --delete-vault   # its vault folder too (only a VM's own folder)
```

It deletes the VM, its baseline and `config/vms/staging.env`; it keeps its volumes
(`--delete-volumes` deletes the ones only it attaches, each asking again), its vault
folder (`--delete-vault`), its logs and SSH keys. It refuses while the VM has
checkpoints, and refuses the VM this checkout uses without `--vm`, a running VM or
one a session holds unless you add `--force`. `--yes` never confirms it; without a
terminal, `--confirm staging` does. The typed name guards against a slip, not
against a program: anything that can run a command in a terminal can type it, so
Claude passes `--confirm` only for a VM you named for deletion. It never deletes
another VM's VM, baseline or copies, or a folder that is or holds another VM's; it
refuses while any other settings file can't be read cleanly or any VM's folder is in
the runner's VM storage (whichever VM is deleted, with or without flags), and it
checks everything again after you confirm. `reset`, `restore`, `checkpoint` and
`baseline` check the same before they delete or replace anything ([SECURITY.md](../SECURITY.md#more-than-one-vm---vm-name-metacomputer-vm)).

The `metacomputer` MCP server drives the VM a bare `metacomputer` means. To give a
Claude Code session the second VM instead, start it with `MCOMP_VM` set (for the
commands it runs) and an MCP configuration whose `metacomputer` server passes the
same variable (the benchmark runs its sessions this way):

```json
{"mcpServers": {"metacomputer": {"type": "stdio",
  "command": "/path/to/metacomputer/bin/metacomputer", "args": ["mcp-lazy"],
  "env": {"MCOMP_VM": "staging"}}}}
```

```bash
MCOMP_VM=staging claude --strict-mcp-config --mcp-config staging-mcp.json
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

### Learn from your own sessions

- "What do you keep getting wrong on the virtual computer? Show me the evidence."
- "Approve the no-frame lesson."
- "Do my lessons actually help? Measure it on the test computer."

```bash
metacomputer learn report                # mine your sessions: proposals with their evidence
metacomputer learn review                # go through them, at your terminal
metacomputer learn approve no-frame      # keep one (Claude approves only the IDs you name)
metacomputer learn list --instructions   # exactly what every new session gets
```

`report` reads this Mac's Claude Code sessions that used the VM, with the action
and event logs, and counts failed calls by class, the same failure again in a
session, routes changed without checking why, and time per app. It proposes
short lessons from built-in templates, each with its evidence. Proposals are built
only from counts and fixed names, so nothing a page or document in the VM says can
become a lesson, and lessons in your own words (`learn add`, `learn edit`) need
your own terminal. Approved lessons reach every new session's MCP instructions as
advice, after the skill's own habits: they can't switch off a guard, override a
refusal or overrule you, and they go to the model like the rest of the
instructions. In a chat, the plugin's `/metacomputer:learn` runs the report and
walks you through the proposals.

`learn bench --config TEST_ENV` runs the benchmark with and without your lessons
on a test VM. It says how many sessions it will run and asks first, since it
spends your Claude usage. Its first run, with two built-in lessons, showed no
measurable difference either way: the benchmark's tasks already pass with
today's skill, which holds the same rules
([without](../bench/runs/learn-2026-11-18-without.json),
[with](../bench/runs/learn-2026-11-18-with.json)). Lessons are for what your own
sessions keep running into. [learn.md](learn.md) has the details.

### Measure a change: the benchmark

- "Run the benchmark on the test computer and compare it with the baseline."

[bench/](../bench/README.md) is 20 desktop tasks across Calculator, Mousepad,
Writer, Calc, Thunar, Firefox, Chrome and the shell. Each runs in a fresh headless
Claude Code session on a test VM, and a check reads the VM's real state afterwards
(saved files, what the browser really sent to a local server, app settings)
rather than trusting what the agent says. It reports pass rate, a partial score,
cost, turns, time and the mix of tool calls, so you can change one thing (the
skill, the shell channel, the driver, the model, the screen size) and see what
moves. The method follows Microsoft Research's Hybrid Computer Use study.

```bash
export MCOMP_CONFIG=/path/to/test-vm.env   # a test VM: each task resets it to its baseline
bench/bench.py run --run-id baseline       # all 20 tasks
bench/bench.py compare baseline shell-forbid-1
```

It refuses to run without `MCOMP_CONFIG`, and refuses the VM named
`metacomputer` unless you pass `--allow-main-vm`. Each run spends Claude usage,
within per-task and per-run budget caps, and a full run takes about 25 to 35
minutes. Every run cited in these docs has its per-task results in
[bench/runs/](../bench/runs/).

## Isolation and safety in detail

The [README](../README.md#security-in-brief) sums up the protections. This is the
detail behind them; [SECURITY.md](../SECURITY.md) says where each one ends, and
[architecture.md](../architecture.md) has the design.

Compared with running Lume and Cua Driver as they come, metacomputer sets up
the following. These are **always on**:

| Protection | What it does |
| --- | --- |
| **mcomp-vm** (the installer's default) | Lume attaches your Mac's clipboard, its speakers and microphone, and Rosetta to every VM. [mcomp-vm](https://github.com/metavert-io/metacomputer-vm) is metacomputer's build of Lume, made by Metavert, not by Cua and not endorsed by them. It's Lume 0.7.2 plus `--no-share-clipboard`, `--no-share-audio`, `--no-share-rosetta` and `--no-host-sharing`, and metacomputer starts the VM without all three unless you turn one on. `metacomputer status` reports what the VM actually has, read from inside it. |
| **Public internet only** | The VM's network is a user-mode gateway on the Mac (Cua's `cua-netgw`) that checks every connection the VM opens. It refuses your Mac, your LAN and router, Tailscale and CGNAT addresses, link-local and cloud-metadata addresses, and outbound mail ports. Blocked attempts are logged. |
| **Nothing listens off loopback** | SSH and the live view are forwarded only to `127.0.0.1` on your Mac. Lume's own VNC server, which listens on every interface, is never started. The live view is a password-protected VNC server inside the VM, and the VM's firewall admits it, and SSH, only from the gateway. |
| **SSH by role** | Keys only, with the VM's host key generated on your Mac and pinned before the first boot. A key per role: `mcp` can only start Cua Driver's MCP server (a forced command, no shell), `ops` is a shell for the desktop user, `admin` is for maintenance, `fill` can only run the vault's fill program, and `proxy` can only open one port on the VM's loopback for the vault's API proxy. The VM reads every account's keys from root-owned files in `/etc/ssh/metacomputer-keys/` (and the vault accounts' own), never `~/.ssh`, so nothing running as the desktop user can add a key or lock your Mac out by wiping its home folder. The Mac's ssh ignores your SSH config and forwards nothing to the VM: no agent, no X11, no ports. |
| **Narrow accounts** | The desktop user has no sudo. After the build, the maintenance account can run only one root script with a fixed list of verbs, cloud-init is turned off, and its cached user-data (which held the host key) is deleted. All accounts have locked passwords. |
| **Verified inputs** | The Ubuntu image (signed `SHA256SUMS`, pinned signing key and image digest), the Cua Driver release (pinned SHA-256, cross-checked against the release's checksums and checked again inside the VM), Lume (pinned checksum, or mcomp-vm's pinned tag and commit), and the Mozilla and Google repository keys (pinned fingerprints), and rclone for S3 volumes (signed checksums, pinned key and digest). The VM never downloads Cua components itself. |
| **Less in the guest** | No snaps. No clipboard agent (`spice-vdagent` is removed) and the virtio sound driver is blacklisted, a second layer for when standard Lume attaches those devices. No shared storage unless you attach some ([storage.md](storage.md)). Cua Driver's telemetry and update checks are off, and so is Lume's telemetry. Browser policies turn off telemetry and password saving in both browsers, and sign-in and sync in Chrome. Security updates install automatically, without automatic reboots. |
| **Page text is data** | The skill and the MCP server's own instructions tell Claude that pages, documents, window titles, accessibility text and shell output from the VM are data, never instructions. Claude never asks you for a password: you sign in through the live view yourself, or keep the credential in the vault and unlock it in your own terminal. `metacomputer web open-untrusted` opens a link in a throwaway Chrome profile without clicking anything on it. |
| **The VM is treated as hostile** | What comes back from the VM is bounded and checked. `metacomputer sh` reads a capped amount of output under one deadline, accepts only an answer that fits a strict schema, and escapes control characters before they reach your terminal. The hub caps every message from the driver. Files copied to the Mac arrive as plain files and folders only (links, devices and unsafe names are left out, and nothing is written through a link), and a copy stops before it can fill your disk. Event text from the VM never goes into a message, only into cleaned data. |
| **Every guard on every path** | One hub per VM holds the only connection to Cua Driver, and Claude Code's tools, `metacomputer call` and scripts all go through it, so its guards apply to each of them. One session controls the desktop at a time. Chrome gets real input, never background typing. Bounded mode's limits hold. The coordinate guard refuses a pixel click when certain window-layout changes happened since the screenshot it was aimed from (the window resized, or covered by a dialog or another app). It compares window geometry, not content: a page that scrolled or redrew inside the same window isn't caught, and when it has no screenshot to compare with it leaves the click to Cua Driver's own checks ([SECURITY.md](../SECURITY.md#the-hub-and-its-guards-mcp_hub-coord_guard)). A refusal is reported to you, never worked around. |
| **Recordings out of the VM's reach** (the default) | A desktop recording streams to your Mac as it records (`RECORD_TO=host`), so a run that wipes or doctors the VM can't take back what was already recorded, and the copy stops before your disk falls below its reserve. Cua Driver's per-task recording is written in the VM: while a task folder is open, each finished step is copied to the Mac every 15 s, once, and `task finish` never replaces it; the newest step and the video arrive at `task finish` ([SECURITY.md](../SECURITY.md#recordings)). |
| **Your Mac stays yours** | Nothing here can see or control your Mac's screen. The VM's wallpaper and menu say VIRTUAL COMPUTER, so every screenshot and recording shows which machine it is. |

These are **opt-in**. They're off by default, so out of the box metacomputer
behaves the way Cua Driver does on its own. Turn on any of them in
`config/local.env`; `metacomputer security` shows what's on, and
[Turn on the optional hardening](#turn-on-the-optional-hardening) shows how.

| Setting | What it adds |
| --- | --- |
| `ACTION_LOG=on` | A signed, hash-chained log of every tool call sent to the VM, and of every `metacomputer sh` run. Each record carries the hash of the one before it, and the chain's head is signed with an SSH key (`ssh-keygen -Y sign`) every 20 records, every minute with new activity, and at the end. By default, typed text is recorded as its length, and scripts and shell output as sizes and hashes. `metacomputer actions verify` finds edited, inserted, reordered or removed records. |
| `SSH_AUTH=cert` | A fresh key and a 5-minute SSH certificate for every connection, limited to one role and to the Mac's side of the VM network. Its key ID (`mcomp:ROLE:USER:PROJECT:pidN`) appears in the VM's auth log. The static `ops` and `mcp` keys stop working, and with them sealed browser sessions (`secrets session save` and `load`), for now. |
| `DRIVER_MODE=bounded` | Cua Driver's bounded mode: every call must fit a capability manifest of tools, apps, folders and websites, with a time limit. Profiles `readonly`, `desktop` (no terminal) and `browser` (only the sites you list), or your own manifest. The Mac asks for the manifest by its SHA-256, and the VM refuses a manifest that doesn't match and, while bounded, any standard session. The shell channel is off while bounded (`VM_SHELL=auto`), since a shell isn't held to the manifest. |
| An event sink | `metacomputer events sink add` sends events as they happen; with no sink, nothing leaves the Mac. [events.md](events.md) has the details. |

SECURITY.md covers three more in full: the vault
([secrets and logins](../SECURITY.md#secrets-and-logins-metacomputer-secrets),
with how it [compares with AWS KMS, Secrets Manager and HashiCorp Vault](../SECURITY.md#if-you-know-aws-kms-secrets-manager-or-hashicorp-vault)),
[the shell channel](../SECURITY.md#the-shell-channel-metacomputer-sh) and
[the event stream](../SECURITY.md#event-stream-metacomputer-events-optional).

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
It reads each account's keys from a root-owned file (`/etc/ssh/metacomputer-keys/` for
`cua` and `cuaadmin`), never from a home folder ([SECURITY.md](../SECURITY.md#ssh-keys-outside-the-home)).
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
- Anything still in the VM can be deleted by what runs there: a desktop recording with `RECORD_TO=guest` until it stops, and a per-task recording's newest step and video until `task finish`.
- `ACTION_LOG_TEXT=keep` records typed text, scripts and shell output in full; keep such logs as safe as the passwords that might be in them.
- Event sinks' URLs, tokens and signing keys sit in a file anything running as you can read. Use ingest-only tokens.

## Known limitations

**Platform:**
- Apple-silicon Macs only, with an ARM64 guest.
- One controller per VM at a time (see [Start it and check on it](#start-it-and-check-on-it)). A second VM needs its own settings file (`metacomputer vm create NAME` writes it).
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

**Sites with bot checks:** some sites refuse the VM's automated browser, even when you tick "Verify you are human" yourself in the live view: page-mode and hybrid Chrome are driven through DevTools, which bot checks detect. Claude hands such steps to you (`metacomputer handoff`) and never tries to get around them; if a site keeps refusing, do that step on your own computer ([When Claude needs you](#when-claude-needs-you-handoffs-and-the-live-view)).

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
| "Paused: the user has the computer" | A handoff is open (`metacomputer handoff status`): Claude waits for you. Run `metacomputer handoff done` when you've finished in the live view, or `handoff cancel`. `release` doesn't end it. |
| Screen Sharing rejects the password | It wants the live-view password, not your Mac's. `metacomputer view --open` brings the password Screen Sharing remembers up to date; `view --open --no-remember` puts it on the clipboard for 45 s instead (paste with Cmd+V). |
| `view --open` says it needs the Mac's own screen | You're in over SSH. Run it in Terminal on the Mac's screen (in person, or through Screen Sharing or another remote-desktop app), or use the SSH tunnel `metacomputer view` prints, or `view --tailnet`. |
| A site's "Verify you are human" check keeps failing or looping | The site is refusing the VM's automated browser (page-mode Chrome is driven through DevTools). Do that step on your own computer; Claude won't try to get around it ([Known limitations](#known-limitations)). |
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

