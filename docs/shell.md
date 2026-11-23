# The shell channel: `metacomputer sh`

`metacomputer sh` runs a bash script inside the virtual computer as its
desktop user and returns the output, the exit code and the duration. It's the
programmatic channel beside the desktop tools.

Microsoft's Hybrid Computer Use research found that agents given code and
command-line tools alongside accessibility and GUI input are both more
accurate and much cheaper. Its guide routes each piece of work by where the
state lives: files no running app holds open go to the shell, while open
documents, settings, signed-in sites, canvases and accounts go to their apps.
The skill's [SHELL.md](../skills/metacomputer/SHELL.md) has that routing for
Claude; this page covers the design.

Before this, the only way in was `metacomputer ssh ops 'cmd'`: one quoted
command, `~` expanded on the Mac, no desktop environment, and output that
arrived only after every program it started had let go of it.

## Usage

```
metacomputer sh -c 'COMMANDS' | --file SCRIPT | -   [options] [-- ARGS...]

  -c 'COMMANDS'        the script as one argument
  --file SCRIPT        a script file on the Mac
  -                    the script on stdin
  --cwd DIR            folder in the VM to run in (default: the desktop user's home)
  --timeout SECONDS    stop the script after this long (default 120, at most 3600)
  --max-output BYTES   keep at most this much of stdout and of stderr (default 65536, at most 1048576)
  --read-only          a check that changes nothing (see "One controller at a time")
  --override           run even though another session is using the computer
  --json               the result as one JSON object
  --raw                print the output exactly as it came, even to a terminal
  -- ARGS              $1, $2, ... for the script
```

| Exit status | Meaning |
| --- | --- |
| 0-255 | The script's own exit status (bash's 126/127/128+N included) |
| 124 | `--timeout` passed; the script was stopped |
| 125 | Refused, nothing ran: `VM_SHELL`, the bounded driver, another session, an invalid setting, a claim on the computer that couldn't be written, or an action log that couldn't be opened or signed |
| 255 | Couldn't run or finish: the VM is off, SSH failed, no such `--cwd`, a malformed or oversized answer from the VM, or a run whose final log signature failed |
| 130 | Interrupted on the Mac; the script was stopped in the VM too |
| 2 | Usage error |

A script can also exit with 124, 125 or 255 itself; `--json` tells them apart
(`status` is `exited`, `timeout`, `refused`, `cancelled` or `error`, and
`exit_code` is the script's own).

`--json` gives `status`, `exit_code`, `exit` (this command's status),
`stdout`, `stderr`, `stdout_bytes`, `stdout_sha256`, `stdout_truncated` (and
the same for stderr), `duration_s`, `cwd`, `read_only`, `desktop` (whether the
desktop's environment was loaded), `held_open` (a program the script left
running still had its output), `max_output`, `logged` (the action-log file,
if any), `message`, and when they apply `kind` (`timeout`, `protocol`,
`connection`, `guest`, `log`, `interrupted`, `vm_off`), `notes`, `warnings`
and `remote` (the VM's own diagnostics).

On a terminal, control characters in the output (but newline and tab), C1
controls and bidirectional overrides are shown escaped (`\x1b`, `\u202e`), so
the VM can't move the cursor, rewrite the screen or set the clipboard (OSC
52). `--raw` prints them as they came. Output to a pipe or a file, as Claude
Code reads it, is always exact. Messages from the command are always
escaped, since they can quote the VM.

## How it works

```text
Mac: bin/metacomputer sh ──▶ lib/vm_shell.py ──ssh -T (ops key or certificate, as `metacomputer ssh ops`)──┐
       settings, one-controller check, action log                                                       │
VM:  python3 -I -c '<read a length, then that much Python, and run it>'   ◀── stdin: lib/vm_shell_guest.py,
       └─ bash --noprofile --norc /tmp/mcsh.XXXX/script ARGS                 a JSON header, the script
            (own process group) stdout, stderr ─▶ pipes, read as they come
       ◀── one JSON line: status, exit code, duration, each stream's size, SHA-256, start and end
```

- **No quoting anywhere.** The remote command is a fixed one-line bootstrap.
  The VM's half of the program, a JSON header (folder, timeout, cap,
  arguments) and the script all travel on SSH's stdin. The VM's half reads the
  header with a 1 MB cap before it parses it. The header can also carry `env`,
  up to 16 more variables for the script's environment, with names only from an
  allowlist (`MCOMP_CLAUDE_*`, `CLAUDE_*`, `ANTHROPIC_*`, so never `PATH`,
  `BASH_ENV`, `LD_*` or the like) and text values. Both halves check it.
  `metacomputer claude` hands its token over that way, so it's in no script,
  argument or command line, and not in the action log (`sh` itself never sets
  it). The Mac's side of `sh` is also
  `vm_shell.run_guarded()`, which `metacomputer claude` runs its steps through.
- **The desktop's environment.** The script gets `DISPLAY`, `XAUTHORITY`, the
  session bus and the XDG variables from the session file the desktop
  publishes at login (`$XDG_RUNTIME_DIR/metacomputer/session.env`), plus the
  accessibility settings every desktop app gets
  (`/etc/X11/Xsession.d/90metacomputer-env`: `GTK_MODULES`,
  `SAL_USE_VCLPLUGIN`, ...). So an app the script starts opens on the desktop
  and Cua Driver reads it like any other. `~/.local/bin` is on `PATH` when it
  exists. If the desktop session isn't up, the script still runs, without
  those, and the command says so.
- **The script** is written to a fixed path, `script`, alone in a private
  folder (0700, removed afterwards), so `$0` is that path whatever the file
  was called on the Mac.
- **Output in pipes, kept bounded.** The VM's half reads stdout and stderr as
  they come, hashing all of each and keeping only its first and last
  `max_output / 2` bytes: nothing is written to disk, memory stays at most
  1.5 times the cap, and a huge output never crosses SSH. The Mac trims the
  cut to whole UTF-8 characters and marks what was left out.
- **Programs left running.** After the script's own process ends, its output
  is read for half a second more. A program it started with `&` that still
  holds the output is then handed to a `cat` that discards what it prints, so
  it neither holds up the answer nor dies of a closed pipe; `held_open` says
  so. Programs started with `setsid -f APP ... >/tmp/APP.log 2>&1` aren't
  affected.
- **Timeout.** One loop in the VM's half watches the script's process, the
  deadline and its output; the deadline holds even if the script closes its
  output (`exec >/dev/null`). It sends SIGTERM to the script's process group,
  keeps reading its output (so a TERM handler that prints a lot isn't
  blocked), and sends SIGKILL 3 s later to whatever of the group is left. The
  answer comes only once the whole group is gone, not just bash. Programs
  started with `setsid -f` have their own session and keep running. Programs
  started with `&` stay in the group: they keep running after a normal end,
  but a timeout stops them.
- **Interruption.** The Mac keeps SSH's stdin open until the answer arrives.
  If it closes first (Ctrl-C, the command killed, the connection lost), the
  VM's half stops the script the same way, and exits only once the group is
  gone. On the Mac, interrupts that arrive while the connection is being
  ended are noted, not acted on, so ssh is always killed, reaped and its
  pipes closed, and the result is still logged and printed.
- **Leftovers.** A run folder left by a killed runner is removed by the next
  run after a day.

### A VM that misbehaves

The VM is untrusted, and so is every program in it running as the desktop
user: one could take over the connection's other end, or stand in for the
VM's half. Whatever comes back, the Mac stays safe:
- **One deadline,** `--timeout` plus 30 s, starts before ssh does. Writing the
  request and reading the answer are both non-blocking and checked against
  it, so a VM that stops reading, or never answers, can't hold the command
  past it. Past it, or on Ctrl-C, the connection is closed (124 or 130).
- **Wire limits.** stdout can carry at most what a well-formed answer can
  hold: each stream's kept bytes as base64, plus 16 KB of JSON. stderr (ssh's
  and the VM half's own diagnostics) is limited to 64 KB. Past either limit
  the connection is closed and nothing is shown (255).
- **A strict schema.** The answer must be exactly one line of JSON with
  exactly the expected fields and types: a known status, an exit code from 0
  to 255 (and only when the script exited), a finite duration, true or false
  where expected, a 64-digit hex SHA-256, and base64 of exactly the size the
  stream's byte count and the cap call for, with lengths checked before
  anything is decoded. Duplicate keys, NaN and extra lines are rejected.
  Anything else is a protocol error (255), logged as one, and nothing from
  the answer is shown. Rejection messages are fixed text, never the VM's.
- **Escaped on a terminal,** as above.

What the VM reports, the sizes and hashes included, is its own account.
Under this trust model they don't prove what ran or what it printed: a
compromised VM can report anything that fits the schema.

## Settings

| Setting | Values | Default | |
| --- | --- | --- | --- |
| `VM_SHELL` | `auto`, `on`, `off` | `auto` | `auto` is on, except while `DRIVER_MODE=bounded`; `on` is on even then; `off` refuses every run |
| `SHELL_LOG` | `auto`, `off` | `auto` | `auto` logs each run when `ACTION_LOG=on`; `off` never logs shell runs |

`metacomputer security` shows both, with what they come to. An unknown value
for either makes `metacomputer sh` refuse to run, rather than guess.

### Why bounded mode turns it off

Bounded mode makes Cua Driver do only what a capability manifest lists:
chosen apps, folders and sites, and (in the `desktop` profile) no terminal.
A shell in the same account isn't held to that manifest. It could open an
unlisted site with `curl`, write outside the listed folders, or start the
terminal the profile leaves out. So `VM_SHELL=auto` means off while the driver
is bounded. `VM_SHELL=on` turns it back on for a user who wants both; the
driver stays bounded and the shell doesn't.

## One controller at a time

A script can disturb what another session is doing on the desktop: kill its
app, change a file it has open, take the focus. So a run counts as acting,
like a click:
- it's refused (125) while another session acted within 10 minutes
  (`metacomputer who`), unless given `--override` (after the user agrees);
- it claims the computer for the caller (`.run/owners/`), so other sessions'
  acting calls and `stop`/`reset` are refused in turn. The command ends with
  the run, so the claim is held by the caller's Claude Code process, or
  outside Claude Code by the shell that ran it; it's refreshed every minute
  of a long run, from the holder found at the start (a file write, without
  reading the process table, so it can't hold up the deadline). That shell
  is often short-lived (`bash -c`, a script, another agent's tool runner), so
  a claim made outside Claude Code lasts the 10-minute activity window from
  its last run even after its shell exits, and runs from the same terminal
  session (its session id) count as the same controller; a run from another
  session is refused until the window has passed (or `--override`).
- the check and the claim are one step under this VM's owners lock
  (`.run/owners/.lock`, `flock`), so two runs started together can't both
  pass. If the claim can't be written, the run is refused (125); if a
  refresh fails, the command says so in a warning. Claims are written through
  a temporary file of their own, so two runs under one session don't collide.
  Claude Code's bridge doesn't take the lock yet: it checks from a cache that
  is at most 3 s old, so a shell run and an acting tool call from two
  sessions starting in the same moment can still both pass.
  `owners.lock()` is there for it.

`--read-only` is for a second session that only needs to look: read a saved
file, list a folder, check a process. It isn't refused and claims nothing.
It runs without the desktop's environment (no `DISPLAY`, no session-bus
address), so a program can't open on the desktop by accident. It's a promise,
not a sandbox: the script can still change files or signal processes, like
`metacomputer ssh ops` can. The skill says to use it only for commands that
change nothing; `metacomputer inspect` and `metacomputer get` remain the
read-only tools by construction.

## The action log

With `ACTION_LOG=on` and `SHELL_LOG=auto`, each run (and each refused run)
writes its own file in `logs/actions/DAY/`, in the same signed, hash-chained
form as a driver connection ([SECURITY.md](../SECURITY.md)):

| Record | Holds |
| --- | --- |
| `start` | who ran it (`via: "shell"`), from which project, the VM and its settings |
| `call`, tool `vm_shell` | the script's keyed hash (`script_hmac`) and size, how it was given (`command`, `file`, `stdin`), `cwd`, timeout, cap, `read_only`, `override`; the script and its arguments only with `ACTION_LOG_TEXT=keep`, otherwise their length |
| `checkpoint` | a signature over the call, made before anything runs |
| `result`, tool `vm_shell` | status, `kind`, exit code, duration, `desktop`, `held_open`, and each stream's full size and keyed hash (`hmac`; and whether it was capped); a message written by the Mac; the VM's own diagnostics (its error text, ssh's stderr) as `remote` size and keyed hash. The output text and diagnostics as text (`remote_text`) only with `ACTION_LOG_TEXT=keep` |
| `refused` | the call's arguments as above and why |
| `end`, final `checkpoint` | the signature over the whole file |

| `ACTION_LOG` | `SHELL_LOG` | `ACTION_LOG_TEXT` | A run is |
| --- | --- | --- | --- |
| `off` | any | any | not logged |
| `on` | `off` | any | not logged |
| `on` | `auto` | `redact` (default) | logged: keyed hashes and sizes, no script or output text |
| `on` | `auto` | `keep` | logged with the script, its arguments and the output text |

What stays in plain text with `ACTION_LOG_TEXT=redact`: the folder in the VM
(`--cwd`) and the `--file` name on the Mac (paths, like a driver call's
paths), the Mac folder it was run from, the VM's name and settings, and
refusal reasons, which can name another session's project folder. Everything
the script contains, its arguments, everything it printed and everything the
VM said in its own words is a size and a keyed hash.

If logging applies, the log must open and sign the call before anything
runs; otherwise the run is refused (125) rather than run unlogged. If the
final signature fails, the command exits 255 and says the run is unlogged,
though the script did run and its output is shown. A signer that doesn't
answer within 10 s is stopped and counts as a failure. `metacomputer actions show` prints a run as
`-> vm_shell {...}` and `<- vm_shell exit 0 (82 ms; stdout 18 B, stderr 0 B)`;
`metacomputer actions verify` checks these files like any other, and a
desktop recording's key moments include shell runs made during it.

The output hashes let you check a file you have against what the VM said a
run printed, without keeping the text in the log. They're keyed: an HMAC-SHA256
of the text's SHA-256 (`sha256:` and its hex digest) under a per-install secret
beside the log's signing key (`action-log.results-hmac` in the VM's SSH folder,
0600, made with the log's first run; the hub's result fingerprints use it too).
A plain SHA-256 of a short output (a code, a number) could be found by trying
every candidate; the keyed one needs that file. They are the VM's report, not
proof (see "A VM that misbehaves"), though an output that came back whole must
match the SHA-256 the VM gave for it.

## Tests

- `python3 tests/test_vm_shell.py` (no VM): the Mac's and the VM's halves run
  locally with `bash -c` standing in for SSH, and a fake, hostile VM floods,
  stalls, stops reading and sends malformed answers.
- `MCOMP_CONFIG=test-vm.env python3 tests/test_options.py F`: the same things
  against a running test VM, plus a detached GUI app the driver can read and
  the isolation checks. See [TESTING.md](../TESTING.md).
