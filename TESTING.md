# Testing

Everything here runs against your own VM, so start it first: `metacomputer start`.
Results and recordings go to `results/`, `recordings/` and `screenshots/`
(all ignored by git).

## GUI suite: `metacomputer test [A-F]`

Each test drives the VM only through Cua Driver's MCP tools (the same bridge
Claude Code uses), records an MP4 and per-action traces, and checks its
outcome independently: the accessibility tree, the driver's `verify_state`,
and files read back from the VM's disk.

| Test | What it does |
| --- | --- |
| A | Observes the desktop: apps, windows, trees, a dialog |
| B | GNOME Calculator: 137 × 29 by pressing its buttons; reads 3973 back |
| C | Firefox: example.com, a second tab, a Wikipedia search through the site's own form |
| D | LibreOffice Writer: types and styles a document, saves it, checks the file |
| E | Several apps together: copy text between apps, save, verify |
| F | Chrome through the driver's page tools: form (text, dropdown, check box, upload), download and rename, a confirm dialog, key-by-key typing |

`results/<run>/summary.md` lists each check.

## Chrome guard: `python3 tests/test_chrome_guard.py`

This test goes through `metacomputer mcp-lazy`, the bridge Claude Code sessions use. It checks four things:
- background input aimed at Chrome is sent in the foreground;
- `set_value` and `run_script` aimed at Chrome are refused;
- a `run_steps` batch aimed at Chrome is sent in the foreground;
- the driver's leftover virtual input devices are removed before Chrome needs the keyboard.

Background is in
[docs/chrome-investigation.md](docs/chrome-investigation.md).

## Host sharing: `metacomputer status`

With lume-secure and the default settings, the `host sharing` line must read
`clipboard off  audio off  rosetta off`. It's read from inside the VM: virtio
console port names, virtio device ids and virtiofs tags. Turning one setting
`on` (e.g. `SHARE_CLIPBOARD=on` in `config/local.env`) and restarting must
show just that one; all three `on` is Lume's own default. The flag parsing
and device selection have unit tests in
[lume-secure](https://github.com/metavert-io/lume-secure).

## Recordings: `tests/test_recording.py`

`python3 tests/test_recording.py` needs no VM. It makes a short video and an
action log with calls before, during and after it, and on another VM, then checks
the description file: settings, length, size and hash, and that the key moments
are exactly this VM's calls during the recording at the right times.

`MCOMP_CONFIG=test-vm.env python3 tests/test_recording.py --vm` also records six
seconds on a running test VM in each mode: `RECORD_TO=guest` at standard quality,
20 fps, and `RECORD_TO=host` at high quality, 30 fps. It checks that a second start
is refused, the frame rate and size, the description file, that nothing is left
behind on the Mac, and that only the guest mode leaves a copy in the VM. It also
checks that a `RECORD_DIR` whose parent is missing (a drive that isn't
connected) is refused rather than created, and that a `RECORD_AUDIO` other than
`auto` or `off` is refused. It reads whether the VM has the virtual sound card
(`VIRTUAL_AUDIO=on`) and checks that each recording has a sound track exactly
when it does; on such a VM it also checks that `RECORD_AUDIO=off` leaves it out.
Without a VM, it checks that the description file describes a clip's sound track
and reports none for a silent one.

Sync between sound and picture was measured separately, by recording a full-screen
flash and a beep started on the same frame once a second. The pulse input's
timestamps run 55-144 ms ahead of the picture (median about 86 ms over 13 takes),
steady within a recording but different between recordings. `desktop-record`
delays the sound by 90 ms, so expect it within about 50 ms of the picture: fine
for watching, but nudge it in the editor if a sound has to land on an exact frame.

## Event stream: `tests/test_events.py`, `tests/test_options.py E`

`python3 tests/test_events.py` needs no VM (169 checks, about a minute). It keeps
everything in a temporary folder and runs a local receiver for each kind: an
HTTP server for webhook, OTLP and ntfy, and a TLS syslog server with its own
CA, made with `openssl` (the syslog checks are skipped without it). It checks:
- sinks are added, listed, shown and removed; the config is private; header values and ntfy topics are never printed; plain http off the Mac, syslog without TLS and credentials in URLs are refused;
- four sinks receive the same events in order, in each format: CloudEvents with a valid HMAC signature, OTLP/HTTP JSON, RFC 5424 over TLS with octet counting, ntfy;
- family and minimum-severity filters, and the opt-in `actions` family;
- cleaning: control characters and escape sequences, URL queries and credentials, secret-looking keys, size caps, a sink's header value and the live-view password;
- the spool, including invalid and oversized files;
- a receiver that fails, then the backlog arriving in order once it's back; a sink that never answers costs at most about 3 s; the backoff;
- eight processes emitting at once; debounce; `notify`;
- the watcher's pieces (blocked connections batched a minute at a time, health changes), `events watcher start|stop`, and the bridge's hooks (VM in use, the Chrome guard, repeated bounded-mode refusals, controllers);
- the relay's `actions` events without arguments or typed text, a failed `actions verify`, and `web open-untrusted` findings by origin only;
- the bash helper, including a broken config that must not fail the command, and the exit hooks for `metacomputer secrets` ("vault locked") and builds;
- the review's fixes, one or more checks each:
  - malformed addresses in `open-untrusted`;
  - query redaction whatever the query holds, also without a scheme;
  - the driver's and the VM's text kept out of messages;
  - the rate limit and its summary; slower repeats of blocked connections; quiet health flapping; repeated `notify` and `emit`;
  - sink changes refused without a terminal, asked at one, and announced to the existing sinks (the removed one included);
  - more secret-looking keys; signing keys and topics redacted; lone surrogates and invisible characters;
  - odd gateway records; a failing watcher step; ports out of range; one watcher per VM;
  - temporary spool names; events left at a bridge's exit; a lost sequence counter; debounce only after recording;
  - what `--replace` says; `origin`; an ntfy `?auth=`; a refused event inside an OTLP batch; the byte cap; `sink test` behind a slow backlog.
- the second review's fixes:
  - reserved types refused from `events emit` and the spool; strict sliding windows; the phone cap per sink (10 in 10 minutes, 30 an hour, tests included); the relay's `actions` outside the family limit;
  - a hand edit to `events.json` announced once to the previous sinks, and its removal too;
  - a sink-change announcement kept for a sink that's down, delivered later, and given up (and logged) for one that never answers;
  - replacing a sink waits for a sender holding its lock; a sender with the retired generation sends nothing;
  - fixed messages for failed recordings, blocked destinations that aren't addresses and `open-untrusted`;
  - every link in a word, bare IPv4, `[IPv6]` and `localhost`, credentials anywhere, encoded links;
  - camelCase, hyphenated and invisible-character key names; no stored tag of the live-view password;
  - a spool file too deep to parse, and folders that don't use up the 500;
  - events started but not finished at a bridge's exit; a restored counter;
  - provenance on the phone; a syslog reset at close counted as a failure.
- the third review's fixes:
  - the phone cap taken right before each attempt: retries use slots, a backlog after an outage arrives as the window allows, what waits isn't dropped; sink-change announcements under the cap, several as one message, waiting without using up attempts;
  - a broken `events.json`: the local log still gets events, status says why, it's announced; the same sinks put back quietly; journal recovery (a prepared announcement sent when its change was written, dropped when it wasn't, including a change cut off before the write); a hand edit journaled before it's accepted;
  - an add or replace confirmed after someone else changed the same sink is refused;
  - spool files: a FIFO swapped in doesn't block, a link isn't followed, a `.quarantine` link isn't used;
  - a trailing newline in a type or sink name refused; no internal `_emit`; `notify` shown as "[from Claude]" on a phone;
  - the watcher wanted while an announcement waits, even with no sinks;
  - an encoded secret key name (`pa%73sword`) and an encoded `token=` after a query;
  - `events-state/redact.json` from an earlier build deleted, and noted.

Sink changes in these tests run on a pseudo-terminal with `--yes`, since they refuse without a terminal.

`MCOMP_CONFIG=test-vm.env python3 tests/test_options.py E` runs against a
running VM, with its own temporary events folder and a local webhook receiver,
so your own sinks get nothing. It changes nothing in the VM (a test page in
`/tmp` is removed afterwards). It checks:
- `metacomputer _health` (the watcher's probe) answers without MCP;
- `record start` / `record stop` sends `task.recording_stopped` with the recording's details (the test recording is deleted afterwards);
- `notify` sends `needs_you.login` with the action;
- a blocked connection from the VM (`curl` to `10.255.255.254:9`) reaches the receiver as `security.blocked_connections` within one watcher cycle (if a watcher already runs for that VM, the test drives one cycle with `events watch --once` instead);
- `web open-untrusted` on `tests/fixtures/site/signin.html` sends `security.untrusted_link` with the password field and origins only;
- nothing the receiver got holds a URL query or the live-view password.

## Optional hardening: `tests/test_actionlog.py`, `tests/test_options.py`

`python3 tests/test_actionlog.py` needs no VM (16 checks). It writes logs to a
temporary folder, then tampers with them: an edited record, an edited record
with its hash recomputed, a deleted record, a cut-off end, and a log signed
with another key. It also runs the relay against a fake driver and checks that
messages pass through unchanged.

`MCOMP_CONFIG=test-vm.env python3 tests/test_options.py [A] [B] [C] [F]` runs against
a running **test** VM. It switches the VM's SSH and driver settings through the
maintenance account, then puts them back to the defaults.
- **A** (6 checks): a real session is logged in order, typed text is redacted,
  the file is private, and `actions verify` passes.
- **B** (12 checks): certificates work. These are refused: the static key, an
  expired certificate, one for another role, one from another CA, and one from
  another address. The VM forces the driver command for `mcp` even when the
  certificate doesn't, key IDs reach the VM's auth log, and the static keys
  work again afterwards.
- **C** (21 checks):
  - `readonly` refuses clicks, typing and launches.
  - The VM refuses a standard session while bounded.
  - `desktop` launches Mousepad by name and types into it. It refuses the
    terminal, page tools, and writes outside the task folders.
  - `browser` opens a listed origin and refuses another one and desktop input.
  - An expired manifest and one changed inside the VM are refused.
  - Standard mode works again afterwards.

- **F** (26 checks), the shell channel (`metacomputer sh`, below).

The GUI suite also runs under the `desktop` profile: put
`DRIVER_MODE=bounded` in the test VM's config and run
`metacomputer test A B C D`.
- E reads the copied passage with `clipboard_read`, which bounded mode refuses
  in driver 0.52.1.
- F uses page tools and hybrid typing, which need standard mode (or the
  `browser` profile for the page tools alone).

## The shell channel: `tests/test_vm_shell.py`, `tests/test_options.py F`

`python3 tests/test_vm_shell.py` needs no VM (97 checks). `bash -c` stands in
for SSH, so the Mac's half (`lib/vm_shell.py`) and the VM's half
(`lib/vm_shell_guest.py`) both run locally; a fake, hostile VM stands in for
the rest. It checks:
- argument parsing and usage errors, guest paths for `--cwd`;
- `VM_SHELL` (`auto` off while bounded, `on`, `off`, unknown values refused)
  and `SHELL_LOG` against `ACTION_LOG`;
- the output cap: whole under the cap, first and last halves over it, sizes
  and SHA-256 of the whole stream, memory bounded, cuts on UTF-8 character
  boundaries; 30 MB of output counted and hashed with nothing on disk;
- exit codes (the script's, 124, 125, 255, 130, 2), stdin, `--file` (any
  name, including `stdout`, runs from one fixed path) and arguments, `--json`;
- the desktop session's environment from a session file, and none with
  `--read-only`;
- a background program doesn't hold the result and isn't killed by a closed
  pipe, a timeout ends the script's process group (after its members' own
  SIGTERM handling), a flooding command is cut off, an interrupted command
  stops the script, and a FIFO or `/dev/zero` link planted in the run folder
  changes nothing;
- the hostile VM: endless stdout, endless stderr and one endless line are cut
  off at the wire limit; a VM that stops reading stdin or never answers is
  given up at the deadline; 28 malformed answers (wrong types, exit code 256,
  NaN, bad hashes or base64, sizes that don't match, duplicate keys, extra
  lines, deep nesting) are rejected with fixed text; end to end they exit 255,
  show nothing and are logged as signed protocol errors;
- control characters reach a terminal only escaped (checked on a
  pseudo-terminal), `--raw` passes them, a pipe gets exact bytes, and the
  VM's diagnostics are escaped in messages;
- another session's claim refuses a run, `--read-only` and `--override` still
  run, and a run claims the computer; the check and claim wait for the owners
  lock; concurrent claim writes don't collide; a claim that can't be written
  refuses the run, and one that can't be refreshed is a warning;
- the VM half's lifecycle: a script that closes its output still times out
  on time; a SIGTERM handler that prints 300 KB finishes because output is
  still read during the grace; on cancellation a child that ignores SIGTERM is
  killed before the VM half exits;
- interrupts arriving all through the Mac's cleanup (a transport that ignores
  SIGTERM and SIGINT): ssh is still killed and reaped, exit 130;
- a signer that hangs (a fake `ssh-keygen`): refused at the start, exit 255
  at the end, within the bound, the signer killed, the failure sticky;
- a claim refresh doesn't read the process table, and the deadline is checked
  again right after a slow one;
- `who`: a bridge's own connection through the action-log relay isn't counted
  as another controller (a fake bridge, relay and ssh process tree), while a
  relay under another client is;
- the action log: no file with `ACTION_LOG=off` or `SHELL_LOG=off`; with
  logging, a `vm_shell` call signed before anything runs and a result with
  hashes and sizes, nothing of the script, its output or the VM's diagnostics
  unless `ACTION_LOG_TEXT=keep`, refusals and timeouts recorded, a log that
  can't be signed at the start refusing the run, a failed final signature
  failing the command, and `actions verify --all` passing.

`MCOMP_CONFIG=test-vm.env python3 tests/test_options.py F` (26 checks) does the
same against a running test VM. It also checks that the script runs as the
desktop user without sudo, that nothing of the Mac's is mounted and the
gateway refuses it, that the desktop's display, session bus and accessibility
settings work, and that a GUI app started with `setsid -f` outlives the run
with an accessibility tree the driver can read. It changes no VM settings: the
`VM_SHELL`, bounded-driver and logging cases are config variants on the Mac.

## SSH options: `tests/test_ssh_options.py`

`python3 tests/test_ssh_options.py` needs no VM and connects nowhere (44
checks). For each role (`admin`, `ops`, `mcp`) with static keys and with
certificates, it builds the arguments `scripts/lib.sh` uses for every
connection to the VM and checks:
- `-F /dev/null` comes first, so no ssh config file applies;
- agent, X11, port and tunnel forwarding, connection sharing, local commands
  and proxies are turned off, and no forwarding flag is present;
- the real `ssh -G` accepts them and resolves them safely, also with a hostile
  config read in place of `/dev/null`;
- `metacomputer sh` hands its transport exactly these arguments (captured
  through a stand-in Python, without connecting).

## Secrets vault: `tests/test_secrets.py`, `tests/test_vault_ops.py`, `tests/test_secret_fill.py`

Both need no VM, and every value in them is a dummy.

`python3 tests/test_secrets.py` (127 checks, about 50 s) builds two copies of
`mcomp-vault`:
- a release build, which must not contain the test switches;
- a test build that runs a stub instead of `ssh` (`scripts/build-vault.sh --test`).
  It refuses to run without its own `MCOMP_VAULT_DIR` outside `~/Library`, and
  `metacomputer secrets` refuses it.

The test types the passphrase, values and approvals into a pseudo-terminal, as a
person would. It checks that:
- `init`, `set`, `bind` and `unlock` refuse without a terminal, and refuse short or
  mismatched passphrases, bad sites and names, and the wrong passphrase;
- `bind` shows what it pins, pins nothing without `yes`, refuses paths with control or
  invisible characters, and (through `bin/metacomputer`) warns while the VM's private
  host key is still on the Mac;
- the vault file is 0600 and holds no names or values in the clear, and a header with
  absurd KDF rounds is refused;
- while locked, `list` and `fill` exit 3 and nothing reaches the VM;
- a port served by anything but the pinned gateway program is refused. A stand-in
  `cua-netgw`, compiled by the test, passes, and is refused once its file's contents
  change; both refusals say to bind again;
- a fill goes to the fill account (`mcfill`) with the fill key, and its value
  reaches the VM side only on ssh's standard input, never on the command line or in
  the environment;
- ssh runs with the pinned port, host key and account, and no config or agent;
- a VM without the fill account says to rebuild;
- the TOTP code is the current one;
- wrong field and kind combinations are refused before anything is sent;
- `each` approval:
  - `y` and wrong words refuse, and send only a check without the value;
  - the question shows the Chrome profile and the selector (one line, at most 80 characters);
  - the secret's name allows, and only the approved site goes with the value;
  - a check naming another site is refused;
  - the listener is checked again after approval;
  - no answer times out, and a question can't outlast the unlock;
- inside tmux (and with `$WEZTERM_PANE` or `$KITTY_LISTEN_ON`), `--confirm each` won't
  start and confirmations are refused unless `--allow-multiplexer`;
- control and bidi characters from the VM or a hostile request reach no output,
  terminal or log, and a VM answer holding the value (even a 6-digit code) is withheld;
- sealed sessions:
  - save and replace always ask, and load asks unless the passphrase-gated policy
    says otherwise;
  - loading into another VM asks, naming both, and loading under another profile name
    is refused;
  - a changed file is refused;
  - the loader refuses `..`, absolute paths, entries outside the profile, links leading
    outside, special files and a symlinked profile folder;
- Ctrl-C, the time running out and the terminal closing each lock the vault;
- through `bin/metacomputer` with `ACTION_LOG=on`, the fill is in the signed log
  and verifies;
- no value appears in any command's output, the unlock terminal or the audit log.

`MCOMP_VAULT_TEST_BIN=PATH` uses an existing test build, and
`MCOMP_VAULT_SKIP_RELEASE=1` skips the release build. When the test itself runs inside
tmux or screen, its ordinary unlocks pass `--allow-multiplexer`.

`python3 tests/test_vault_ops.py` (176 checks, about 2 minutes) reuses that stub and
pseudo-terminal for the rest of the vault:
- **keys:** `init` binds to the Secure Enclave (when the Mac has one) and prints a
  recovery key; `--no-enclave`; `rekey` (the old passphrase and recovery key stop
  working); on a Mac whose Enclave can't use the key (a test-build switch), only the
  recovery key opens the vault, and `recover`, then `rekey --enclave`, move it back; a
  changed header breaks every wrap; a `set` made while `rekey` builds its keys (a
  test-build pause) is kept;
- **rotation:** `set` over a secret needs `--replace`; `list` shows ages;
  `rotate-session-key` re-encrypts sealed sessions, which still load; cut short twice
  in a row (a test-build switch), both sessions still load and a third run finishes;
  an unreadable sealed session stops it with nothing changed;
- **audit:** the hash chain, creation MACs (`audit --verify`; a locked refusal has none
  and is marked unverified), the unified log; `scripts/verify-audit.py` in both modes
  (a changed, removed, repeated or reordered local line fails; raw events in place of a
  signed capture fail; repeated deliveries pass; `--expect-start` and `--expect-head`;
  `--require-verified`);
- **forgeries:** a record rewritten (and rehashed) after the vault wrote it, a forged
  record without a MAC and one with a wrong MAC each reach the sink as an
  `audit_tamper` notice, never signed as genuine; a forged `sinks/NAME.state` is ignored;
- **audit sinks, all four at once** against local receivers: webhook (CloudEvents,
  signature, user header), OTLP (`/v1/logs`, attributes, a header typed with `{ask}`),
  syslog over TLS with a throwaway CA passed with `--ca` (and refused without it), and
  ntfy (JSON publishing, its URL typed with `ask`). Each gets every event since it was
  added, in order, identical to the local log; captures verify with their own key and
  fail with another's; an optional sink that's down stops nothing and catches up in
  order; a required one refuses uses until it's back, even with its saved state forged
  to look caught up, and stops the very next proxied request when it goes down; an
  audit log that can't be written refuses uses while a sink is required; a `--ca`
  syslog sink still checks the host name; removing one leaves the others;
- **requesters** through a stand-in process named `claude`: the prompt and audit name
  its project and pid; `NAME session`, `NAME unlock` and plain `NAME` last as long as
  they should; `--project` limits;
- **API tokens** against a local plain-HTTP upstream (a test-build switch): the real
  header swapped in (Bearer and `x-api-key`), the key withheld from echoed answers
  (also split across reads), path limits, redirects, hop-by-hop headers, expiry,
  `revoke`, a VM without the proxy account, the tunnel's ssh arguments, the action log
  holding only the token's id, and the proxy and tunnels gone after the vault locks
  (a `rekey` while unlocked); tokens ended by `set --replace` (no new value, no wider
  path) and `delete`; the request parser refusing overflowing chunk sizes, huge or
  doubled Content-Length, CL with TE, other transfer codings, bare CR/LF, NULs, folded
  headers, absolute URIs, CONNECT and HTTP/2.0, with the unlock process still serving;
- **the events spool:** `approval_waiting` while a question waits, `vault_locked` from
  `bin/metacomputer` on exit 3, file names and a folder made if missing.

`python3 tests/test_secret_fill.py` (163 checks) covers the VM side,
`provisioning/guest/rootfs/opt/metacomputer/bin/secret-fill`, in three parts.
- **Its helpers:**
  - sites and field types;
  - `/proc/net/tcp` and `tcp6` parsing, against fixtures with decoys;
  - DevTools `/json/version` answers and Chrome command lines;
  - WebSocket frames, including RFC 6455's examples;
  - redaction.
- **The whole program against a fake Chrome DevTools server**, found through a
  `/proc/net/tcp` fixture beside a plain web server, a silent listener and another
  user's port. The fake models:
  - frames with loader ids, isolated worlds, `DOM.describeNode` and `SystemInfo`;
  - a tab list, or a page's own world, that disagrees with the frame tree;
  - cross-origin, srcdoc, sandboxed and out-of-process frames;
  - several tabs;
  - profiles found from the browser's command line (`SystemInfo.getInfo`), from its
    pid and `/proc`, or from `Browser.getBrowserCommandLine`, and a failure that
    says what each route answered;
  - a reload between the check and the typing (nothing typed) and a navigation during
    it (reported);
  - the keyboard route when a page won't take `execCommand`;
  - focus moving or lost, length limits and invalid selectors;
  - no known desktop user (nothing filled), and a hung browser (the deadline).
- **The whole program against a real headless Chromium**, when one is found
  (`MCOMP_TEST_CHROME`, Google Chrome, or Playwright's copy). It:
  - fills a dummy login page, through the element itself, with fields in a same-origin
    frame, a srcdoc frame, a cross-origin frame and a shadow root;
  - finds the profile from the browser's own command line, with no `/proc`;
  - checks a page that redefines `type`, `activeElement` and `self.origin` in its own
    world.

`MCOMP_CONFIG=test-vm.env python3 tests/test_options.py D` checks the fill and proxy
accounts on a VM built with this version, changing nothing. It covers:
- the fill account's forced command, whatever is asked for;
- no terminal and no forwarding for it;
- the proxy key opening its one reverse forward (a request in the VM reaches the Mac),
  bound to the VM's loopback only; any other port refused; no command (nologin), no
  terminal, no local forwarding;
- that the fill, proxy, ops and desktop keys don't cross;
- that the accounts, their programs and key lists are root-owned and their homes empty.

What these can't cover is the VM's own Chrome, launched by the driver; see the
manual steps below.

### In the VM (manual)

1. Build and install the vault (`scripts/build-vault.sh`), and build a VM with this
   version.
2. In your own terminal, outside tmux:
   - `metacomputer secrets init`;
   - `metacomputer secrets set demo --kind password --origin http://localhost:8000 --username demo`;
   - `metacomputer secrets bind`: check the fingerprint in the live view, then type `yes`.
3. Serve `tests/fixtures/site/` in the VM (below) and open
   `http://localhost:8000/login.html`, a dummy sign-in page, in page-mode Chrome
   with a named profile.
4. `metacomputer secrets unlock --confirm each` in your terminal. From Claude, or
   another terminal: focus the field, then run
   `metacomputer secrets fill demo --field password --profile P`.
5. Check:
   - the question names the page's site and the profile, and typing `demo` allows it;
   - with `--profile`, a failure lists what each route answered;
   - the field fills and the result shows `chars`;
   - submitting the page shows only the lengths of what was typed.

   Then `session save` / `session load` that profile.
6. API tokens (needs a VM built with this version, bound again):
   - `metacomputer secrets set httpbin --kind api --api-host httpbin.org --header 'Authorization: Bearer {value}'`
     (type any dummy key of 8+ characters);
   - `metacomputer secrets token httpbin` and, in the VM, `curl -s -H "Authorization:
     Bearer <token>" http://127.0.0.1:18765/httpbin/anything`: httpbin echoes the
     headers it received, and the `Authorization` header there reads `Bearer [key
     withheld by metacomputer secrets]`, the proxy having blanked the real key it sent;
   - `metacomputer secrets revoke --all`, and the same request gets 401.

## Browser checks: `metacomputer web …`

`compare`, `responsive`, `a11y` and `open-untrusted` serve as their own
smoke tests. The pages in `tests/fixtures/site/` cover the edge cases:
- a form with every control;
- a page with planted accessibility problems;
- a redirecting fake sign-in page;
- a dummy sign-in page for `metacomputer secrets fill` (`login.html`).

To serve them in the VM:
`metacomputer put tests/fixtures/site --to '~/metacomputer-runs'`, then
`metacomputer ssh ops 'cd ~/metacomputer-runs/site && python3 -m http.server 8000 --bind 127.0.0.1'`.

## The skill, end to end: `tests/skill/run_headless.sh`

This runs a headless Claude Code session from any folder, with only the
`metacomputer` tools and `metacomputer` allowed, and summarizes what it did:
- the skill it used;
- the commands and tools it called;
- screenshots versus structural reads;
- the checks it ran;
- cost and the result.

```bash
tests/skill/run_headless.sh results/skill-tests OUT_NAME /some/other/folder "Get yourself a virtual computer and tell me what's on its screen."
```

Prompts worth covering:
- "use a computer to …";
- "in Chrome, drive the page directly";
- "check this page in both browsers";
- "what does this link do?";
- "take a screenshot of my screen", which must **not** use the VM;
- plain questions, which must not boot it.

Each session costs real API usage.

## Agentic check: `tests/agentic_claude.sh`

A headless session with only the MCP tools does a recorded calculator task by
reasoning over trees and screenshots, with no scripted steps.

## Benchmark: `bench/bench.py`

Twenty desktop tasks (Calculator, Mousepad, Writer, Calc, Thunar, Firefox,
Chrome, a data task and a multi-app task), each run in a fresh headless
session on a test VM that is reset to its baseline first. A check reads the VM's
real state afterwards. Each task records pass, partial score, cost, turns,
time, tokens and the tool-call mix. Switches (skill, shell channel,
coordinate guard, screen size, model) change one thing at a time, and
`bench/bench.py compare A B` puts two runs side by side. See
[bench/README.md](bench/README.md). Each run costs real API usage: about $1 per
task, and it needs a test VM (`MCOMP_CONFIG`).
