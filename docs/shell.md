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
  -- ARGS              $1, $2, ... for the script
```

| Exit status | Meaning |
| --- | --- |
| 0-255 | The script's own exit status (bash's 126/127/128+N included) |
| 124 | `--timeout` passed; the script was stopped |
| 125 | Refused, nothing ran: `VM_SHELL`, the bounded driver, another session, an invalid setting, or an action log that couldn't be opened |
| 255 | Couldn't run or finish: the VM is off, SSH failed, no such `--cwd` |
| 130 | Interrupted on the Mac; the script was stopped in the VM too |
| 2 | Usage error |

A script can also exit with 124, 125 or 255 itself; `--json` tells them apart
(`status` is `exited`, `timeout`, `refused`, `cancelled` or `error`, and
`exit_code` is the script's own).

`--json` gives `status`, `exit_code`, `exit` (this command's status),
`stdout`, `stderr`, `stdout_bytes`, `stdout_sha256`, `stdout_truncated` (and
the same for stderr), `duration_s`, `cwd`, `read_only`, `desktop` (whether the
desktop's environment was loaded), `max_output`, `logged` (the action-log
file, if any) and `message`.

## How it works

```text
Mac: bin/metacomputer sh ──▶ lib/vm_shell.py ──ssh -T (ops key or certificate, as `metacomputer ssh ops`)──┐
       settings, one-controller check, action log                                                       │
VM:  python3 -I -c '<read a length, then that much Python, and run it>'   ◀── stdin: lib/vm_shell_guest.py,
       └─ bash --noprofile --norc SCRIPT ARGS (own process group)            a JSON header, the script
            stdout, stderr ─▶ files in a private /tmp/mcsh.XXXX folder
       ◀── one JSON line: status, exit code, duration, each stream's size, SHA-256, start and end
```

- **No quoting anywhere.** The remote command is a fixed one-line bootstrap.
  The VM's half of the program, a JSON header (folder, timeout, cap,
  arguments) and the script all travel on SSH's stdin.
- **The desktop's environment.** The script gets `DISPLAY`, `XAUTHORITY`, the
  session bus and the XDG variables from the session file the desktop
  publishes at login (`$XDG_RUNTIME_DIR/metacomputer/session.env`), plus the
  accessibility settings every desktop app gets
  (`/etc/X11/Xsession.d/90metacomputer-env`: `GTK_MODULES`,
  `SAL_USE_VCLPLUGIN`, ...). So an app the script starts opens on the desktop
  and Cua Driver reads it like any other. `~/.local/bin` is on `PATH` when it
  exists. If the desktop session isn't up, the script still runs, without
  those, and the command says so.
- **Output in files, not pipes.** stdout and stderr go to files in a private
  folder (0700, removed afterwards). A program the script leaves running can't
  hold the connection open, and doesn't die of a closed pipe later.
- **The cap.** The VM reads each file once, hashing all of it and keeping its
  first and last `max_output / 2` bytes, so a huge output never crosses SSH.
  The Mac trims the cut to whole UTF-8 characters and marks what was left out.
- **Timeout.** The VM's half sends SIGTERM to the script's process group, then
  SIGKILL 3 s later. Programs started with `setsid -f` have their own session
  and keep running. Programs started with `&` stay in the group: they keep
  running after a normal end, but a timeout stops them.
- **Interruption.** The Mac keeps SSH's stdin open until the answer arrives.
  If it closes first (Ctrl-C, the command killed, the connection lost), the
  VM's half stops the script the same way. The Mac also gives up 30 s after
  the timeout, which closes the connection.
- **Leftovers.** A run folder left by a killed runner is removed by the next
  run after a day.

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
  of a long run.

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
| `call`, tool `vm_shell` | the script's SHA-256 and size, how it was given (`command`, `file`, `stdin`), `cwd`, timeout, cap, `read_only`, `override`; the script and its arguments only with `ACTION_LOG_TEXT=keep`, otherwise their length |
| `result`, tool `vm_shell` | status, exit code, duration, whether the desktop's environment was loaded, and each stream's full size and SHA-256 (and whether it was capped); the output text, as returned, only with `ACTION_LOG_TEXT=keep` |
| `refused` | the call's arguments as above and why |
| `end`, final `checkpoint` | the signature over the whole file |

| `ACTION_LOG` | `SHELL_LOG` | `ACTION_LOG_TEXT` | A run is |
| --- | --- | --- | --- |
| `off` | any | any | not logged |
| `on` | `off` | any | not logged |
| `on` | `auto` | `redact` (default) | logged: hashes and sizes, no script or output text |
| `on` | `auto` | `keep` | logged with the script, its arguments and the output text |

If logging applies and the log can't be opened, the run is refused rather
than run unlogged. `metacomputer actions show` prints a run as
`-> vm_shell {...}` and `<- vm_shell exit 0 (82 ms; stdout 18 B, stderr 0 B)`;
`metacomputer actions verify` checks these files like any other, and a
desktop recording's key moments include shell runs made during it.

The output hashes let you prove later that a file you have is what a run
printed, without keeping the text in the log.

## Tests

- `python3 tests/test_vm_shell.py` (no VM): the Mac's and the VM's halves run
  locally with `bash -c` standing in for SSH.
- `MCOMP_CONFIG=test-vm.env python3 tests/test_options.py F`: the same things
  against a running test VM, plus a detached GUI app the driver can read and
  the isolation checks. See [TESTING.md](../TESTING.md).
