# Security

metacomputer gives Claude Code a desktop of its own: an Ubuntu VM on
your Mac that Claude drives through Cua Driver. The defaults already isolate
it:
- the VM reaches only the public internet, not your Mac or your network;
- nothing listens off your Mac's loopback;
- the VM has no shared folders;
- with lume-secure, the VM doesn't get your clipboard, audio or Rosetta. (`VIRTUAL_AUDIO=on`
  adds a sound card inside the VM that plays into nothing; it never reaches the Mac.)

[README.md](README.md#isolation-and-safety) lists all of it, and
[architecture.md](architecture.md) has the design.

This page covers three **optional** protections. All are off by default, so
out of the box metacomputer behaves the way Cua Driver does on its own.
Each one is independent; turn on any of them in `config/local.env`. It also
covers the shell channel, `metacomputer sh`: what it can reach, how bounded
mode turns it off, and its optional logging.

| Setting | What it adds | Costs |
| --- | --- | --- |
| `ACTION_LOG=on` | A signed, tamper-evident record of every action sent to the VM | Disk space; a few ms per call |
| `SSH_AUTH=cert` | A fresh key and a 5-minute certificate for every connection, named in the VM's log | About 30 ms per connection |
| `DRIVER_MODE=bounded` | Cua Driver does only what a manifest lists: chosen apps, folders and sites | Less freedom: no terminal, no page tools on unlisted sites |

`metacomputer security` shows what is on, and what the VM itself has.
`metacomputer start` brings the VM in line with the config;
`metacomputer security apply` does the same without a restart.

## What these protect against

The main risk with a computer-use agent is **prompt injection**. A web page, a
document or a window title tells the agent to do something you didn't ask for.
The skill treats everything on the VM's screen as data, not instructions, but
no model is immune. The options below are about the next two questions: what
did the agent actually do, and how much could it have done?

They do not protect against someone who already controls your Mac account.
That person can change the config, read the keys and drive the VM directly.
Each section says where its protection ends.

## Signed action log (`ACTION_LOG=on`)

Every connection to Cua Driver goes through `metacomputer mcp`. That
includes Claude Code's bridge, `metacomputer call`, `web` and the tests.
With the log on, a relay there writes one file per connection to
`logs/actions/DAY/TIME-PID.jsonl` (private to you, 0600). The file holds:
- who connected, from which project, and the VM's settings;
- every tool call exactly as it was sent;
- each result's error flag, hash, image count and timing;
- every call the bridge refused.

How it resists tampering:
- **Hash chain.** Each record includes the hash of the one before it. Editing,
  inserting, reordering or deleting a record breaks the chain from that point.
- **Signatures.** About every 20 records, every minute with new activity, and
  when the connection closes, the head of the chain is signed with an SSH key
  (`ssh-keygen -Y sign`).
- **Final signature.** A file whose last signature isn't marked final was cut
  short, or its session crashed.

Typed text, values and scripts are recorded as their length only.
`ACTION_LOG_TEXT=keep` records them in full; only do that when you can keep
the log as safe as the passwords that might be typed. Results are never stored,
only their hash, so a log doesn't copy what was on the screen.

```
metacomputer actions list            # recent sessions
metacomputer actions show latest     # one session, readable
metacomputer actions verify --all    # check every file; exit 1 if anything was changed
```

`metacomputer task finish` also copies the logs written during a task into
its results folder; copies verify like the originals.

**Where it ends:**
- The signing key is `$SSH_DIR/action-log`, created on first use. Anyone who
  can read it can rewrite a log and sign it again. To prevent that, keep the
  key in ssh-agent backed by hardware (a Secure Enclave or security-key agent)
  and point `ACTION_LOG_KEY` at its `.pub` file. The private key then never
  exists as a file.
- Deleting a whole file isn't detected. Copy the files somewhere you control
  if you need that.
- The log covers the driver and `metacomputer sh` (unless `SHELL_LOG=off`;
  see below), not `metacomputer ssh`.
- It records what was asked and what came back, not what the screen showed.
  Use `metacomputer record` for that.
- A signer that doesn't answer within 10 s (an agent or hardware key waiting
  for a touch or a passphrase) is stopped, and that counts as a failed
  signature from then on.

To verify on another machine, use the public key:
`metacomputer actions verify FILE --key action-log.pub`, or plain
`ssh-keygen -Y verify` with the namespace `metacomputer-action-log`.

## Event stream (`metacomputer events`, optional)

`metacomputer events sink add` sends events to a webhook, an OpenTelemetry
Collector, a syslog server over TLS or ntfy: when Claude is blocked on you,
when a task or recording finishes, security signals (blocked connections, the
Chrome guard, a failed `actions verify`, what `web open-untrusted` found) and
the VM's lifecycle and health. [docs/events.md](docs/events.md) has the
details. Off by default: with no sink, nothing is sent.

**What it protects against, and what it doesn't.** It guards against hostile
text from the VM and from web pages (everything is cleaned, and text from the VM
never becomes a message), against floods and accidents in metacomputer's own
code (limits, debouncing, backlogs that can't grow without bound), and it tells
you about sink changes made through `metacomputer events sink` or noticed on
disk. It does **not** guard against a program running as you, an agent
included: such a program can read `~/.config/metacomputer/events.json` (ntfy
topics, webhook URLs, headers, signing keys) and post to your destinations
directly, or edit the configuration and the state folder. Noticing hand edits is
best effort. The secrets vault's audit sinks are different: their credentials
live inside the encrypted vault.

**What is sent:** tool names, counts, file paths on the Mac, project folder
paths, the VM's and the Mac's short names, and origins (scheme, host, port) of
web pages. **What isn't:** typed text, passwords, tokens, page contents,
screenshots, URL queries and fragments, the live-view password, or sink header
values. Every string is cleaned before it is logged or sent, including events
other programs hand in through the spool. Text that comes from inside the VM
(the driver's refusals, its health report) never goes into a message, only into
`data`, cleaned.

**Sink changes are announced:**
- `events sink add` and `remove` (and `--replace`) work only from a terminal and
  ask first. That guards against mistakes and programs without a terminal, not
  against one running as you.
- Every change made that way is announced as `security.sink_added` or
  `sink_removed` to the local log and to every sink that existed before it, the
  removed or replaced one included, at its old address. The announcement is
  journaled before the configuration is written, so an interruption announces
  what was written and drops what wasn't, and a change that raced another one
  is refused. Each announcement is retried (up to 20 times, with backoff) until
  that sink has it, so a sink that's down or being removed still hears about it.
- An edit to `events.json` made any other way is noticed when it can be
  (metacomputer keeps a hash and a copy of the last configuration it wrote),
  journaled and announced as `security.sink_config_changed` to the sinks of that
  last known configuration. A file that can't be read stops sending until it's
  fixed; `events status` and the local log say why. A program that also edits
  the state folder can hide its change.

**Noise limits:** at most 10 events per family in any 10 minutes reach the
sinks (a strict sliding window); the rest are in the local log and summed up in
one `suppressed` event. Each phone (ntfy) sink gets at most 10 messages in any
10 minutes and 30 in any hour, whatever they are (tests, summaries, retries and
sink-change announcements included), checked right before each send; what has
to wait isn't dropped, it goes as slots free up. Only metacomputer's own code
paths can put an event outside the family limit; `events emit` and spool files
can't. Repeated blocked-connection reports slow down (up to once an hour), a
flapping health condition is announced once per 30 minutes, and `notify`
doesn't repeat itself within 5 minutes.

**Telling metacomputer's events apart:** each event's `origin` (`cli`,
`bridge`, `watcher`, `spool`, `claude`, `script`) is set by the code path that
made it. `notify` says `claude` and on a phone starts with "[from Claude]";
`metacomputer events emit` (your scripts, and metacomputer's own shell hooks)
says `script` and starts with "[sent from the command line]"; spool files say
`spool`, "[from another program]". The types only metacomputer's own code makes
(the watcher's, the bridge's, sink changes, summaries) are refused from `emit`
and the spool. This sorts metacomputer's code paths; it doesn't authenticate
anyone: anything running as you can call `emit`, or post to a sink directly.

**Where it ends:**
- The sink configuration (`~/.config/metacomputer/events.json`, 0600) is
  readable by anything that runs as you, Claude included. Give sinks
  ingest-only, low-scope tokens, or keep the real credentials in a relay or a
  local OpenTelemetry Collector. `sink list` and `sink show` never print
  header values.
- The live-view password is redacted when it's kept in a file. When it lives in
  your own secret store, the event code never fetches it and keeps nothing
  derived from it; but the VM knows it, so a hostile VM could slip it into a name
  it controls. One earlier build of this branch (f615a13) kept a short verifier
  of it in `events-state/redact.json`; current builds delete that file.
- Claude doesn't add or remove sinks: when you ask, it gives you the command to
  run in your own terminal, with `--header-env` when a token is involved.
- Webhook signatures cover the body, not the time of sending: drop repeated
  event ids to stop replays (docs/events.md).
- It is best effort, at least once, not an audit trail: anyone with your login
  can change its backlog and logs. The signed action log above is the
  tamper-evident record.

## SSH certificates (`SSH_AUTH=cert`)

By default the Mac reaches the VM with two static keys, `ops` (a shell) and
`mcp` (only Cua Driver), both in `$SSH_DIR`. With certificates:

- A CA key, `$SSH_DIR/ca`, is created on first use.
- Every connection gets a fresh key and a certificate for one role, valid for
  `SSH_CERT_TTL` (5 minutes; only the sign-in must fall inside it). The
  certificate is valid only from the Mac's side of the VM network.
- Its key ID (`mcomp:ROLE:USER:PROJECT:pidN`) appears in the VM's auth log for
  every sign-in.
- The VM decides what each role may do, whatever a certificate says.
  `/etc/ssh/metacomputer-principals/cua` forces the driver command for
  `mcp` and allows only a plain shell for `ops`.
- The static `ops` and `mcp` keys stop working. The maintenance account keeps
  its key, so `metacomputer security apply` can always switch back.

The tests check that each of these is refused: the static key, an expired
certificate, one for another role, one from another CA, and one used from
another address.

**Where it ends:**
- A certificate can be used more than once while it is valid. Certificates
  have no single-use mode.
- The CA key is a file by default, so whoever can read it can issue
  certificates. As with the log key, `SSH_CA_KEY` can name a `.pub` whose
  private key lives in ssh-agent.
- What this adds is short-lived credentials and a name on every connection.
  It doesn't add a new barrier against your own account: only your Mac can
  reach the VM's SSH in the first place.

## Bounded driver (`DRIVER_MODE=bounded`)

Cua Driver has a `bounded` permission mode. Every call must fit a capability
manifest that lists:
- the tools it may use;
- the apps it may launch and read (by exact path);
- the folders it may read and write;
- the websites it may open;
- how long the session lasts (`expires_after`, at most 24 hours) and how long
  it may sit idle (`idle_timeout`).

Anything else is refused before it touches the desktop.

Pick a profile with `DRIVER_PROFILE` (the files are in
[config/driver-profiles/](config/driver-profiles/)):

| Profile | Allows | Leaves out |
| --- | --- | --- |
| `readonly` | Looking: windows, accessibility trees, screenshots | Every click, keystroke and launch |
| `desktop` (default) | The installed apps (Firefox, Chrome, Mousepad, Calculator, LibreOffice, Thunar), mouse and keyboard, recordings, files in the task folders, `~/Inbox` and `~/Downloads` | The terminal, browser page tools, extensions, driver settings, the clipboard tools (Ctrl+C and Ctrl+V between apps still work) |
| `browser` | Chrome's page tools in a throwaway profile, only on the sites in `DRIVER_ORIGINS` | Desktop input and screen reads; Cua runs those only in a separate runtime |

`DRIVER_MANIFEST=/path/to/manifest.yaml` uses your own manifest instead. See
Cua's [permissions guide](https://cua.ai/docs/cua-driver/guides/permissions)
for the format. `DRIVER_EXPIRES` (8h) and `DRIVER_IDLE` (30m) set the time
limits.

How it's enforced:
- The Mac renders the manifest and uploads it to the VM. The connection asks
  for it by its SHA-256.
- The VM's driver wrapper refuses a manifest whose contents don't match that
  hash.
- While bounded mode is on, the VM also refuses standard-mode driver sessions
  (`/etc/metacomputer/driver-policy`, owned by root).
- Claude Code's bridge launches apps named like `"firefox"` by their absolute
  path, because bounded mode accepts only paths. When a session expires, it
  tells Claude to ask you to reconnect, rather than reconnecting by itself.

**Where it ends:**
- It limits the driver, which is how Claude acts on the VM. It doesn't limit
  the `ops` shell the Mac uses for status and file copies, or programs already
  running inside the VM. That's why bounded mode turns `metacomputer sh` off
  unless `VM_SHELL=on` (below).
- Cua runs page tools and desktop tools in separate runtimes, so one bounded
  session can't use both. `MODE=hybrid` browsing needs standard mode.
- Driver 0.52.1's manifests have no entry for the clipboard, so
  `clipboard_read` and `clipboard_write` are always refused in bounded mode.
- In our tests with driver 0.52.1, `kill_app` refused to close apps the
  driver had launched, even with `terminate: driver_launched`. Close them from
  their own window, or use `metacomputer desktop-restart`.

## The shell channel (`metacomputer sh`)

`metacomputer sh` runs a bash script in the VM as the desktop user and brings
back its output and exit code ([docs/shell.md](docs/shell.md)). It's on by
default, because code and command-line tools are often the more accurate
and cheaper way to work with files in the VM.

What it can do:
- whatever the desktop user `cua` can do in the VM: read and write that
  user's files, run and stop that user's programs, start apps on the desktop
  (it gets the desktop session's environment), and reach the public internet
  like the VM's browsers do.

What it can't do:
- **Reach your Mac.** It uses the same SSH path, account and key (or
  certificate) as `metacomputer ssh ops`. The Mac's ssh runs with no config
  file (`-F /dev/null`, so no `Host` block in `~/.ssh/config` can add a
  `ForwardAgent`, `RemoteForward`, `ProxyCommand`, `SendEnv` or shared
  connection) and with agent, X11, port and tunnel forwarding, connection
  sharing and local commands turned off on the command line; that holds for
  every connection to the VM (`sh`, `ssh`, the driver, recordings, status).
  The VM's side of the key refuses forwarding too, but a hostile VM's sshd
  wouldn't, so the Mac's side is what counts. The gateway refuses every connection from the VM to the
  Mac, your network and the tailnet, and the VM has no shared folders. The
  script travels on SSH's stdin and is never run by a shell on the Mac.
- **Get root.** `cua` has no sudo.
- **Get around the one-controller rule by accident.** A run counts as acting:
  it's refused while another session is using the computer, unless the user
  agrees to `--override`. The check and the claim are one step under a lock,
  and a run whose claim can't be written is refused. `--read-only` skips
  that for checks, as a promise rather than a sandbox.
- **Harm the Mac through what comes back.** The VM, and any program in it
  running as the desktop user, is treated as hostile
  ([docs/shell.md](docs/shell.md#a-vm-that-misbehaves)). The Mac reads a
  bounded number of bytes per stream under one deadline (a VM that floods,
  stalls or stops reading gets its connection closed), accepts only an answer
  that fits a strict schema, and escapes control characters before they reach
  a terminal (`--raw` opts out), so the VM can't move the cursor, rewrite the
  screen or set the clipboard through OSC 52.

Settings:
- `VM_SHELL=auto` (the default) is on, except while `DRIVER_MODE=bounded`:
  the shell isn't held to the driver's manifest, so it would get around the
  bounded profile's limits. `VM_SHELL=on` allows it anyway; `VM_SHELL=off`
  refuses every run (exit 125).
- `SHELL_LOG=auto` (the default) records every run in the signed action log
  when `ACTION_LOG=on`, as a `vm_shell` call and result: the script's
  SHA-256 and size, the folder, the exit code and duration, and each output
  stream's size and SHA-256. The script, its arguments, the output text and
  the VM's own diagnostics are kept only with `ACTION_LOG_TEXT=keep`, like
  typed text; otherwise they're a size and a hash, and the result's message
  is written by the Mac. In plain text either way: the folder in the VM, the
  `--file` name, the Mac folder it was run from, and refusal reasons.
  `SHELL_LOG=off` keeps shell runs out of the log; with `ACTION_LOG=off`
  nothing is logged either way. Refused runs and malformed answers are logged
  too. The call is signed before anything runs, and a run whose log can't be
  opened or signed is refused; if the final signature fails, the command
  exits 255 and says the run is unlogged.

`metacomputer security` shows both settings and what they come to.

**Where it ends:**
- `VM_SHELL=off` turns off this command, not SSH. `metacomputer ssh ops`
  still works for the Mac's own plumbing (status, `put`, `get`, `inspect`),
  and anyone with your Mac account can use it. The skill tells Claude not to
  use it to get around a refused `sh`, but that's an instruction, not a
  barrier; Claude Code's permission rules can deny `metacomputer ssh` if you
  want one.
- The log records what was sent and hashes of what came back; it doesn't
  record what the script did inside the VM. Programs it started detached keep
  running after the run is logged.
- The sizes and hashes in a result are the VM's own report. They let you
  check a file against what the VM said, but a compromised VM can report
  anything that fits the schema, so they don't prove what ran or printed.
- Claude Code's bridge doesn't take the owners lock yet, so a shell run and
  an acting tool call from two sessions starting in the same moment can both
  pass the check.
- `--read-only` isn't enforced; it only skips the one-controller check and
  leaves out the desktop's environment.

## Reporting a vulnerability

Use **Report a vulnerability** on the repository's Security tab (GitHub
private vulnerability reporting). Please don't open a public issue for a
security problem. Bugs in Cua Driver or Lume themselves belong upstream at
[trycua/cua](https://github.com/trycua/cua).
