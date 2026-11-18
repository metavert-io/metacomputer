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

## Browser checks: `metacomputer web …`

`compare`, `responsive`, `a11y` and `open-untrusted` serve as their own
smoke tests. The pages in `tests/fixtures/site/` cover the edge cases:
- a form with every control;
- a page with planted accessibility problems;
- a redirecting fake sign-in page.

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
