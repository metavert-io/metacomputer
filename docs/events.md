# Event stream (optional)

People who run long or unattended sessions on the virtual computer need to know
when something needs them or matters: Claude is stuck at a sign-in page, a task
finished, the VM tried to reach your network, the VM stopped answering.
`metacomputer events` sends those moments to places you choose: a webhook, an
OpenTelemetry Collector, a syslog server over TLS, or your phone through ntfy.

**It is off by default.** With no sink configured, nothing is sent anywhere
and every command behaves as before. Events then only go to a local log on
this Mac (`logs/events.jsonl`, or your `LOG_DIR`).

The secrets vault keeps its own, stronger audit trail. This stream is
separate and best effort; it doesn't depend on the vault, and the vault doesn't
depend on it.

## What it protects against, and what it doesn't

It **does** guard against:
- hostile text from the VM and from web pages: everything is cleaned, and text
  from the VM never becomes a message, only cleaned data;
- floods and accidents from metacomputer's own code: limits per family and per
  phone, debouncing, backlogs that can't grow without bound;
- sink changes going unnoticed: changes made through `metacomputer events sink`
  are announced to the sinks you already had, and edits to the configuration file
  made any other way are noticed and announced, on a best-effort basis.

It does **not** guard against a program running as you (an agent included).
Such a program can read `~/.config/metacomputer/events.json`, with its ntfy
topics, webhook URLs, headers and signing keys, and post to your destinations
directly; it can also edit the configuration and the state folder, or fake a
terminal. Nothing in this stream can stop that, so don't rely on it for that.
The secrets vault's audit sinks are different: their credentials live inside
the encrypted vault.

## Quick start

```bash
# In your own terminal (adding a sink asks you first, and refuses without a terminal).
# Your phone, through ntfy (pick a long random topic: anyone who knows it can read it)
metacomputer events sink add phone --kind ntfy --url https://ntfy.sh/<long-random-topic> \
  --families needs_you,task
metacomputer events sink test phone      # sends one test event now
metacomputer events sink list            # what's configured, backlog, last error
metacomputer events tail                 # the local log
```

From then on, `metacomputer start` also starts a small watcher process (below)
and `metacomputer status` shows a line like
`events  1 sink, watcher running, backlog 0`.

When Claude is blocked on you (a dialog it won't accept, an approval), the
`metacomputer` skill tells it to run `metacomputer notify "…" --why approval
--action "…"`. For a CAPTCHA or bot check, a sign-in you do yourself or a 2FA
prompt, it runs `metacomputer handoff`, which sends the same notice and pauses
Claude's actions until you're done ([guide](guide.md#when-claude-needs-you-handoffs-and-the-live-view)).
Either reaches every sink that takes the `needs_you` family.

## Commands

| Command | What it does |
| --- | --- |
| `events sink add NAME --kind webhook\|otlp\|syslog\|ntfy --url URL [--header 'Name: value' ...] [--header-env 'Name=VAR' ...] [--ca FILE] [--families F1,F2\|all] [--min-severity info\|warn\|error] [--replace] [--yes]` | Add a sink. Only from your own terminal (below). A webhook sink prints its signing key once. `--replace` overwrites a sink of that name: a new signing key, and its waiting events are dropped (it says how many). |
| `events sink list [--json]` | Every sink: kind, where (with secret-looking path parts masked), filters, backlog, last delivery or error. Header values are never shown. |
| `events sink show NAME [--key] [--json]` | One sink; `--key` also prints a webhook sink's signing key. |
| `events sink test NAME` | Send a test event to that sink now (ignoring its backoff) and say whether it was accepted. |
| `events sink remove NAME [--yes]` | Remove the sink and its backlog. Only from your own terminal. |
| `events tail [N] [--json]` | The newest events in the local log. |
| `events emit FAMILY TYPE --message TEXT [--severity S] [--action TEXT] [--data JSON] [--field K=V ...] [--debounce S]` | Emit an event from a script. Its origin is `script`, and the types only metacomputer's own code makes (below) are refused. The same event again within 60 s is dropped; `--debounce 0` turns that off. |
| `events status [--json\|--line]` | Sinks, watcher, total backlog, failing sinks. |
| `events drain [--now]` | Take the spool and retry every backlog (what the watcher does every 30 s); `--now` ignores the backoff. |
| `events watcher start\|stop\|status` | The watcher by hand (`start` and `stop` do it for you). |
| `notify "TEXT" [--why login\|captcha\|2fa\|dialog\|approval\|other] [--action "what to do"]` | A `needs_you` event: Claude is blocked on a human. The same message again within 5 minutes isn't sent. |
| `handoff --why captcha\|login\|2fa\|other --say "TEXT"`, `handoff done\|cancel` | Claude hands the computer to you: the same `needs_you` event as `notify` (not sent when you ran it at your own terminal), then `lifecycle.handoff_opened`, and `handoff_done`, `handoff_cancelled` or `handoff_timed_out` when it ends. |

Header values usually hold ingest tokens. `--header 'Authorization: Bearer …'`
puts the token in your shell history; `--header-env` takes it from the
environment instead, for example with a secret store that runs a command with
the value set:
`--header-env 'Authorization=Bearer $NTFY_TOKEN'`.

**Adding, replacing or removing a sink needs your own terminal.** These decide
what leaves this Mac, so `events sink add` and `remove` refuse when standard
input isn't a terminal (an agent's shell, a script, a pipe). At a terminal they
ask first; `--yes` skips the question but still needs the terminal.

That is a guard against mistakes and against programs without a terminal,
not against a program running as you (see above). What metacomputer does is tell
you about changes:
- every change made through `events sink` is announced as `security.sink_added`
  or `security.sink_removed` (with the URL masked) to the local log and to every
  sink that existed before the change, whatever their families, the removed or
  replaced one included, at its old address;
- the change and its announcement are one transaction: the announcement is
  written to a journal (with a copy of each target sink as it was) before the
  configuration is, so an interruption either announces what was written or
  drops what wasn't. If the sinks changed while you were answering the question
  (someone else added or replaced the same name), the command refuses and asks
  you to run it again;
- announcements don't go through the sinks' backlogs: each is retried (with
  backoff, up to 20 times) until that sink has it, so a sink that's down or being
  removed still hears about it later. A phone gets them under its cap, several
  waiting at once as one message. Each outcome is written to
  `events-state/admin.log`, and `events status` shows the ones still pending;
- an edit to `events.json` made any other way is noticed the next time anything
  emits or the watcher runs (metacomputer keeps a hash and a copy of the last
  configuration `events sink` wrote), journaled, and announced as
  `security.sink_config_changed` (sinks added, removed or changed, masked) to the
  sinks of that last known configuration. The edited configuration is used from
  then on. If the file can't be read, nothing is sent until it's fixed: events
  still reach the local log, `events status` says why, and that is announced
  too. This is best effort: a program that also edits the state folder can hide
  its change.

## Families and what they emit

`--families all` (the default) means `needs_you,task,security,lifecycle`.
`actions` is never part of `all`; a sink gets it only when you name it.

| Family | Type | Severity | When | Data |
| --- | --- | --- | --- | --- |
| `needs_you` | `login`, `captcha`, `2fa`, `dialog`, `approval`, `other` | warn | Claude ran `metacomputer notify --why …`, or `metacomputer handoff --why …` (not when you ran it at your own terminal) | `why`, `declared_by: claude`; from a handoff also `handoff_id` and `timeout_s`, and its `action` names `metacomputer view --open` and `metacomputer handoff done`. The message is Claude's `--say`, cleaned. |
| `needs_you` | `in_use` | warn | Claude asked for the VM while another controller had it (the bridge refused the call); once per session per 5 min | the tool, the other controllers (project folder, idle time, last tool) |
| `needs_you` | `refused` | warn | With `DRIVER_MODE=bounded`, Cua Driver refused the same tool twice within 5 min; then at most once per tool per 5 min. The message is fixed and names only the tool, since the driver's text comes from inside the VM | tool, count, `limit`: which kind of manifest limit (`app`, `path`, `site`, `tool` or `other`). None of the driver's text is sent. |
| `needs_you` | `vault_locked` | warn | `metacomputer secrets fill`, `secrets session save\|load` or `secrets token` exited 3: the vault isn't unlocked, or doesn't exist yet (an exit hook in `bin/metacomputer`; it takes effect where the secrets vault is installed). Not when you ran the command at your own terminal; at most once in 5 min. The vault may also send its own events through the spool | the action says to run `metacomputer secrets unlock` (or `secrets init`) |
| `needs_you` | `approval_waiting` (and others) | warn | The secrets vault drops these into the spool | whatever the vault puts in `data` |
| `task` | `finished` | info; error when nothing could be copied | `metacomputer task finish` | task, duration, videos (path, seconds), contact sheets, file and screenshot counts, the results folder |
| `task` | `recording_stopped` | info | `metacomputer record stop` saved a recording | name, duration, path, description file, key-moment count, audio yes/no, mode, size |
| `task` | `recording_failed` | error | `record stop` couldn't save it. The message is fixed text written on the Mac | name, mode, and `detail`: what the VM's recorder said, cleaned and cut short |
| `security` | `blocked_connections` | warn | New lines in the gateway's `netgw-blocked.jsonl`. The first blocked connection after 5 minutes with none goes out at once (the watcher looks every 2 seconds), at most 6 times in any hour. What follows it is batched: at most one event a minute, and when the same destinations come back each time, the wait doubles (2, 4, 8 minutes, up to an hour); a different set starts over | count; per destination: host, port, protocol, reason, count, first and last time; `same_as_before`; `first` (true for the one sent at once). Never payloads. |
| `security` | `sink_added`, `sink_removed` | warn | `events sink add`, `--replace` or `remove`, sent to the sinks that existed before the change, kept until each has it (outside the family limit; a phone gets them under its cap, several at once as one `sink_changes` message) | the sink's name, kind, masked URL, families, minimum severity, `replaced` |
| `security` | `sink_config_changed` | warn | `events.json` was changed other than through `events sink` (or can't be read any more); sent to the sinks of the last configuration `events sink` wrote, kept until each has it | `added`, `removed`, `changed` (name, kind, masked URL, families each), `readable` |
| `security` | `chrome_input_refused` | warn | The bridge's Chrome guard refused `set_value` or `run_script` aimed at Chrome; once per session and tool per 5 min | tool (never the value) |
| `security` | `actions_verify_failed` | error | `metacomputer actions verify` found a changed log | files that failed, how many were checked |
| `security` | `lesson_approved`, `lesson_edited`, `lesson_removed`, `lessons_resealed` | warn | `metacomputer learn approve`, `add`, `edit`, `remove` or `reseal` changed what every new session gets in its MCP instructions (docs/learn.md) | the lesson's id, a short title (at most 60 characters), its source (built-in, claude-draft, edited, you), how many are active, `text_hash` (the lesson's text), `store_hash` (the whole store after the change), `store_id` (which store) and `change_id` (the matching entry in `approvals.jsonl`; lessons are sent only while the record's last entry has its event). Never the lesson's text. Outside the family limit: queued to every matching sink and delayed, never held back, while a sink is down (lost only past the backlog's 10,000 events, or if the sink is removed first). The id and hashes also go to `lesson-events.jsonl` beside the local log, which isn't rotated. |
| `security` | `untrusted_link` | warn | `web open-untrusted` found a password field, a download, or that the link ended on another site. The message is fixed words; hosts only in data | summary, start and final origin (scheme, host, port only, and only hosts made of letters, digits, dots and dashes, or IP addresses), password fields, the number of downloads, origins passed through |
| `security` | `hidden_text` | warn when something is instruction-like, else info | The hub marked page text a person likely can't see, or that addresses AI agents, in a result for Claude (`HIDDEN_TEXT=on`; [SECURITY.md](../SECURITY.md#hidden-page-text)); at most once per host per 5 min, and 10 in 10 min from one hub. The message is fixed words and counts | `tool`, `hidden` and `instruction_like` (counts), `signals` (`tiny`, `no_size`, `off_left`, `off_above`, `no_layout`, `off_flow`, with counts), `patterns` (`addresses_ai`, `override`, `secrecy`, `markup`, `exfil`, `spoof`, with counts), `host` (from the driver's address bar or snapshot URL in that same result, letters, digits, dots and dashes only; null when the result doesn't show one, as a query or diff read doesn't), `complete`. Never page text. A scan that was only cut short, with nothing found, sends no event. |
| `lifecycle` | `started`, `stopped`, `reset`, `restored`, `checkpoint`, `baseline`, `build_done`, `build_failed` | info (build_failed: error) | those commands | checkpoint name, what it was restored from |
| `security` | `storage_protection_failed` | error | The folder watcher couldn't protect a read-write folder (too many entries or too deep, an attribute that won't stick, the Mac's disk below `IMPORT_MIN_FREE_GB`, an unexpected error) and stopped the VM ([storage.md](storage.md#folder-volumes)) | the reason and the folders |
| `security` | `symlink_neutralized`, `special_file_removed`, `quarantine_restored` | warn | The folder watcher, while a VM has a Mac folder read-write ([storage.md](storage.md#folder-volumes)): links the VM made that lead out of the folder (replaced by a note), FIFOs, sockets or devices it made (removed), and the quarantine attribute it removed or changed (put back); at most one of each per folder per pass | the volume and folder, and up to 10 paths (with each link's target), cleaned |
| `task` | `s3_conflict` | warn | An S3 volume's sync found files changed on both sides since the last one ([storage.md](storage.md#s3-volumes)); both copies were kept | the volume and up to 10 file names, cleaned |
| `task` | `s3_sync_failed` | error | An S3 volume didn't sync at start, on the interval or at stop (the network, the keys, or rclone's safety stop when every file changed) | the volume, when, and rclone's last error line, cleaned (never the keys) |
| `lifecycle` | `storage_attached` | info | `start`, once the VM has mounted its volumes and reference disks ([storage.md](storage.md)) | each item's kind, name, mount point in the VM, `rw` and Mac path |
| `lifecycle` | `controller_connected`, `controller_released` | info | A Claude Code session's first acting call; the session ending; `metacomputer release` | bridge and Claude Code process ids |
| `lifecycle` | `handoff_opened`, `handoff_done`, `handoff_cancelled`, `handoff_timed_out` | info (timed out: warn) | `metacomputer handoff` gave you the computer (Claude's acting calls wait), and how it ended: `handoff done` or Enter, `handoff cancel` or Ctrl-C, or its time limit. Sent once, by the process that made the change. The message is fixed words | `handoff_id`, `why`, `timeout_s`; when it ends, `open_s`, `ended_from` (`terminal`, `enter`, `ctrl_c`, `claude_session`, `process`, `time_limit`) and `has_note`. Never the note itself. |
| `lifecycle` | `health` | warn or error; info when it clears | The watcher's probe every 5 min, one event per change. A condition that comes back within 30 min of being announced stays quiet (and so does its recovery), so a flapping one doesn't ping you every probe. Nothing from inside the VM reaches the message or data, not even a number: the VM's report becomes fixed categories (the doctor's check failing or not reporting a result, under 3 GB free), since a VM could spell a value it was given in an "exit code" or a disk size | `condition`: `vm_off`, `gateway`, `ssh`, `not_ready`, `doctor`, `disk_low` (under 3 GB), `recording_failed`; `state`: `problem` or `ok` |
| any | `suppressed` | info | The family limit held events back (below); sent before the next one that goes through, or by the watcher | how many, the limit |
| `lifecycle` | `test` | info | `events sink test` | |
| `actions` | `start`, `call`, `result`, `refused`, `checkpoint`, `end`, `sign_failed` | info (refused, sign_failed: warn) | Each record of the signed action log as it's written (`ACTION_LOG=on`) | record type, sequence number, hash and previous hash, file name, tool, error or refused, time taken. Never arguments or typed text. |

`actions` is for teams who want every tool call in their SIEM; send it to an
OTLP or syslog sink, which batch. These events skip the local log, since the
action log already has them.

## The event

Every sink gets the same event, in its own wrapping:

```json
{
  "id": "0b6e2f4e-5c1a-4f57-9d58-2a8c1e7d3b90",
  "seq": 42,
  "time": "2026-11-17T18:04:05.123456Z",
  "family": "needs_you",
  "type": "login",
  "severity": "warn",
  "origin": "cli",
  "vm": "metacomputer",
  "project": "~/Projects/my-app",
  "message": "Sign in to example.com is needed to download the report",
  "action": "Sign in through the live view (metacomputer view), then tell Claude to carry on",
  "data": {"why": "login", "declared_by": "claude"},
  "host": "my-mac"
}
```

| Field | |
| --- | --- |
| `id` | A UUID per event. Delivery is at least once, so use it to drop duplicates. |
| `seq` | A number per user on this Mac, increasing. Each sink receives events in this order. |
| `time` | UTC, RFC 3339 with microseconds. |
| `family`, `type`, `severity` | As in the table above; severity is `info`, `warn` or `error`. |
| `origin` | Which code path made it, set by that code and never by the caller: `cli` (a metacomputer command's own code: task finish, record stop, actions verify, open-untrusted, sink changes), `bridge` (Claude Code's bridge or the action-log relay), `watcher`, `spool` (another program), `claude` (`metacomputer notify`, Claude's own channel), or `script` (`metacomputer events emit`, used by your scripts and metacomputer's own shell hooks; anything that can run a command can call it). This tells apart metacomputer's code paths; it isn't proof against a program running as you. Types that only metacomputer's own code makes are refused from `events emit` and the spool: `suppressed` in any family, the `actions` family, `needs_you.in_use` and `refused`, `security.chrome_input_refused`, `blocked_connections`, `sink_added`, `sink_removed` and `sink_config_changed`, `lesson_approved`, `lesson_edited`, `lesson_removed` and `lessons_resealed`, `lifecycle.controller_connected`, `controller_released`, `health`, `test`, `storage_attached` and the four `handoff_*` types, and the folder watcher's `security.symlink_neutralized`, `special_file_removed`, `quarantine_restored` and `storage_protection_failed`, the hub's `security.hidden_text`, and `task.s3_conflict` and `s3_sync_failed`. |
| `vm` | The VM's name (`VM_NAME`). |
| `project` | The folder of the Claude Code session that caused it, when known (its working folder); else the command's current folder. In real events this is the full path. |
| `message` | One line for a person. |
| `action` | What the person should do, mostly for `needs_you`; otherwise null. |
| `data` | Small, cleaned details (below). |
| `host` | This Mac's short host name. |

### What is cleaned out

Before anything is logged or sent:
- control characters, terminal escape sequences, bidirectional-text controls, zero-width and invisible characters (U+00AD, U+061C, U+200B-U+200F, U+2028-U+202E, U+2060-U+2064, U+2066-U+2069, U+FEFF, tag characters) are removed, and lone surrogates replaced, in every string;
- links lose any `user:password@`, `;path=parameters`, the query and the fragment, everywhere: every link in a word (`a.example/,https://b.example/...`), from `?` or `#` to the next space whatever the characters, also without a scheme (`site.example/a?token=x`, `127.0.0.1/x#y`, `[::1]:8080/x?y`, `localhost/x?y`), protocol-relative (`//host/a?x`) or `mailto:`-style, credentials before any host (`alice:pw@host`), and the words right after a cut query when they look like more of it (contain `=` or `&`). Words with `%XX` escapes are decoded once before anything else is decided, so an encoded link, or an encoded `token=` after a query, is cleaned too. `https://site.example/a?token=x` becomes `https://site.example/a`;
- keys in `data` that look secret are dropped. Key names are compared in one form first (`%XX` decoded, invisible characters removed, camelCase split, `-`, `.` and spaces as `_`, lowercase), so `signingKey`, `access-key`, `API.Key` and `pa%73sword` count: password, passphrase, passcode, pwd, secret, token, cookie, header(s), credential, session, jwt, bearer, api key, private key, anything ending in `key` or `_code`, sig/signature, auth/authorization, and the exact keys text, value, values, script, typed, otp, totp, code, pin, vnc…;
- these are replaced by `[redacted]` wherever they appear: any sink's header values, webhook signing keys, the secret-looking parts of sink URLs (ntfy topics, webhook paths, query values), and the live-view password when it's kept in `$SSH_DIR/vnc-pass`. When it lives in your own secret store (`VNC_PASSWORD_STORE=command`), the event code never fetches it and keeps nothing derived from it, and no code path puts it into an event; but the VM itself knows it (it runs the live view), so a hostile VM could still slip it into a name it controls, such as a file name in a task's data. One earlier build of this branch (f615a13) kept a short verifier of that password in `events-state/redact.json`; current builds delete that file. If yours lives in your own secret store and that build ran, consider changing the password;
- sizes are capped: message 500 characters, action 300, each string in `data` 300, 50 keys or list items, 4 levels deep, `data` 4 KB (in bytes) in all.

Typed text, passwords, tokens, page contents and the note you leave with
`handoff done --note` are never part of an event in the first place: the hooks
pass tool names, counts, origins and paths.

## Kinds

### `webhook`: CloudEvents 1.0, signed

One HTTPS `POST` per event, in [CloudEvents](https://cloudevents.io) structured
mode:

```http
POST /your/path HTTP/1.1
Content-Type: application/cloudevents+json; charset=utf-8
User-Agent: metacomputer-events/1
X-Metacomputer-Signature: sha256=5c0f…(64 hex)

{"specversion":"1.0","id":"0b6e2f4e-…","source":"metacomputer/my-mac",
 "type":"io.metavert.metacomputer.needs_you.login","time":"2026-11-17T18:04:05.123456Z",
 "subject":"metacomputer","datacontenttype":"application/json","data":{…the event…}}
```

- `type` is `io.metavert.metacomputer.<family>.<type>`, for example
  `io.metavert.metacomputer.task.finished` or
  `io.metavert.metacomputer.security.blocked_connections`.
- `source` is `metacomputer/<host>`, `subject` the VM's name, `id` the event's id.
- The signature is HMAC-SHA256 over the exact body bytes. The key is the 32
  random bytes that `sink add` prints in base64 (and `sink show NAME --key`
  prints again); decode it before use. Compare in constant time:

```python
import base64, hashlib, hmac
def verified(body: bytes, header: str, key_b64: str) -> bool:
    want = "sha256=" + hmac.new(base64.b64decode(key_b64), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header or "")
```

Any 2xx is success. Redirects aren't followed and proxies aren't used.

**Replays.** The signature covers the body only, so a captured request can be
sent again and still verify. It replays the same `id`: keep the ids you've seen
(for a day, say) and drop repeats. You can also reject a `time` older than you
tolerate, but allow for backlogs: an event can arrive late, up to the backoff
cap (10 minutes) plus however long your receiver was down. `--replace` makes a
new signing key; the old one stops working at once.

**Slack, Mattermost, Discord and other chat tools** want their own JSON
(`{"text": …}`), not CloudEvents. Put a small relay in between, which also
keeps the chat tool's webhook URL out of metacomputer's config. For example,
on this Mac:

```python
# relay.py: check metacomputer's signature, post one line to a Slack-compatible incoming webhook.
import base64, hashlib, hmac, json, os, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = base64.b64decode(os.environ["MCOMP_EVENTS_KEY"])   # metacomputer events sink show slack --key
HOOK = os.environ["CHAT_WEBHOOK_URL"]                     # Discord: add /slack to its webhook URL

class Relay(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        want = "sha256=" + hmac.new(KEY, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, self.headers.get("X-Metacomputer-Signature", "")):
            self.send_response(401); self.end_headers(); return
        ev = json.loads(body)["data"]
        text = f"*{ev['vm']}* {ev['family'].replace('_', ' ')}: {ev['message']}"
        if ev.get("action"):
            text += f"\n-> {ev['action']}"
        req = urllib.request.Request(HOOK, json.dumps({"text": text}).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        self.send_response(204); self.end_headers()

HTTPServer(("127.0.0.1", 8789), Relay).serve_forever()
```

```bash
metacomputer events sink add slack --kind webhook --url http://127.0.0.1:8789/ --families needs_you,task
```

Start the relay with both values set from your secret store, and add the sink
before you start the relay, since the key comes from `sink add`.

### `otlp`: OpenTelemetry logs over OTLP/HTTP JSON

`POST <url>/v1/logs` (`/v1/logs` is added unless the URL already ends with it),
`Content-Type: application/json`, up to 50 events per request, following the
protobuf JSON mapping (camelCase names, 64-bit integers as strings):

```json
{"resourceLogs": [{
  "resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": "metacomputer"}},
    {"key": "service.version", "value": {"stringValue": "v0.1.0-12-gabc1234"}},
    {"key": "host.name", "value": {"stringValue": "my-mac"}}]},
  "scopeLogs": [{"scope": {"name": "metacomputer.events", "version": "1"},
    "logRecords": [{
      "timeUnixNano": "1763402645123456000", "observedTimeUnixNano": "1763402645200000000",
      "severityNumber": 13, "severityText": "WARN",
      "body": {"stringValue": "{…the event as JSON…}"},
      "attributes": [
        {"key": "metacomputer.family", "value": {"stringValue": "needs_you"}},
        {"key": "metacomputer.type", "value": {"stringValue": "login"}},
        {"key": "metacomputer.vm", "value": {"stringValue": "metacomputer"}},
        {"key": "metacomputer.seq", "value": {"intValue": "42"}},
        {"key": "metacomputer.project", "value": {"stringValue": "~/Projects/my-app"}}]}]}]}]}
```

Severity: info = 9 `INFO`, warn = 13 `WARN`, error = 17 `ERROR`. Each record also
has `metacomputer.origin`. A URL with a query gets `/v1/logs` before the query.
When the receiver refuses a batch outright (HTTP 400, 413, 415, 422), its events
are retried one at a time, so only the bad one is dropped.
`service.version` is `git describe` of the checkout.

The usual setup is an [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
on this Mac, listening on loopback only, that forwards wherever you keep logs:

```yaml
# otelcol-contrib config: receive on 127.0.0.1:4318, forward
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
processors:
  batch: {}
exporters:
  awss3:                       # S3; a bucket with Object Lock keeps them unchangeable for a retention period
    s3uploader: {region: us-east-1, s3_bucket: my-metacomputer-events, s3_prefix: events}
    marshaler: otlp_json
  # awscloudwatchlogs: {region: us-east-1, log_group_name: metacomputer, log_stream_name: events}
  # googlecloud: {project: my-project}                                    # Cloud Logging
  # splunk_hec: {endpoint: "https://splunk.example.com:8088/services/collector", token: "${env:SPLUNK_HEC_TOKEN}"}
  # otlphttp/loki: {endpoint: "https://loki.example.com/otlp"}            # Grafana Loki 3's OTLP endpoint
  # datadog: {api: {key: "${env:DD_API_KEY}", site: datadoghq.com}}      # or the Datadog Agent's own OTLP intake
service:
  pipelines:
    logs: {receivers: [otlp], processors: [batch], exporters: [awss3]}
```

```bash
metacomputer events sink add collector --kind otlp --url http://127.0.0.1:4318 --families all,actions
```

The Collector holds the cloud credentials, so metacomputer's config needs
none. A hosted OTLP endpoint works too (`--url https://…` with
`--header-env` for its ingest key).

### `syslog`: RFC 5424 over TLS

`--url syslog+tls://HOST[:PORT]` (port 6514 by default). TLS only; plain UDP
or TCP syslog isn't offered. The server's certificate is checked against the
system's trust store, or with `--ca FILE` against that CA alone (the host name
is always checked; X.509 "strict" format rules are relaxed for a private CA).
Messages use octet counting (RFC 5425 / RFC 6587), up to 100 per connection:

```
<12>1 2026-11-17T18:04:05.123456Z my-mac metacomputer - login [metacomputer@32473 seq="42" family="needs_you" vm="metacomputer" id="0b6e2f4e-…"] {…the event as JSON…}
```

- PRI: facility user (1); severity info = 6, warn = 4, error = 3.
- APP-NAME `metacomputer`, PROCID `-`, MSGID = the event's type.
- Structured data `metacomputer@32473` with `seq`, `family`, `origin`, `vm`, `id`.
  32473 is the enterprise number reserved for documentation (RFC 5612).
- MSG is the event as JSON, without a byte-order mark.
- Best effort: syslog over TCP has no acknowledgement, so "delivered" means TLS
  took every byte and the connection closed without a transport or TLS error.
  A reset or a TLS alert while closing counts as a failure, and the batch is
  retried (so the server may get it twice); a server that just closes, or never
  sends its close_notify back, is fine. A server that drops messages after
  taking them isn't noticed.

rsyslog (8.2108 or later, OpenSSL driver) receiving it into a file of JSON lines:

```
global(DefaultNetstreamDriver="ossl"
       DefaultNetstreamDriverCAFile="/etc/rsyslog.d/tls/ca.pem"
       DefaultNetstreamDriverCertFile="/etc/rsyslog.d/tls/server.pem"
       DefaultNetstreamDriverKeyFile="/etc/rsyslog.d/tls/server.key")
module(load="imtcp" StreamDriver.Name="ossl" StreamDriver.Mode="1" StreamDriver.AuthMode="anon")
template(name="msgonly" type="string" string="%msg%\n")
ruleset(name="metacomputer") { action(type="omfile" file="/var/log/metacomputer.jsonl" template="msgonly") }
input(type="imtcp" port="6514" ruleset="metacomputer")
```

```bash
metacomputer events sink add siem --kind syslog --url syslog+tls://logs.example.com:6514 --ca ~/certs/logs-ca.pem
```

Most SIEMs (Splunk, Elastic, Sentinel through a forwarder, QRadar) take RFC
5425 directly.

### `ntfy`: a message for a person

One message per event to an [ntfy](https://ntfy.sh) topic, published as JSON
to the server's root (`{"topic", "title", "message", "priority", "tags"}`):
- title: `<vm> - <family>: <type>`, for example `metacomputer - needs you: login`;
- message: the event's message, then ` -> ` and the action when there is one;
- priority: 5 (max) for `needs_you`, 4 for warn and error, 3 for info;
- tags: one per family (raising_hand, white_check_mark, shield, computer, memo), plus warning or rotating_light.

Messages say where they came from when anything could have sent them:
`[from Claude]` (from `notify`), `[sent from the command line]` (from
`events emit`, including metacomputer's own shell hooks) or `[from another
program]` (from the spool). A `?auth=` query on the topic URL is kept. `https://` only, except to this
Mac's loopback. On ntfy.sh anyone who knows a
topic can read it, so use a long random topic, or your own server or account
with an access token (`--header-env 'Authorization=Bearer $NTFY_TOKEN'`).

## Filtering

Each sink has families (`--families needs_you,task`, `all`, or `all,actions`)
and a minimum severity (`--min-severity warn` drops info). An event goes to
every sink whose filters it passes.

## Delivery

- **At the moment it happens.** The command or bridge that emits the event
  tries each matching sink right away, in parallel, with a 3-second timeout.
  The hub (and `mcp-lazy` with `MCP_HUB=off`) and the action-log relay do this on a background
  thread, so a tool call never waits.
- **Then a backlog.** An event that couldn't be delivered stays in that
  sink's backlog on disk (0600), and the next events queue behind it, so each
  sink receives events in order. A failing sink is retried after 15 s, then 30
  s, doubling up to 10 minutes. Later emissions and the watcher retry it; while
  it's in its backoff it isn't tried at all, so it costs the emitter nothing.
- **Never fatal.** A failing or slow sink never fails or blocks the command
  that emitted. `events sink list` and `events status` show each sink's
  backlog and last error; problems in the event code itself go to
  `events-state/errors.log`, also shown by `events status`.
- **Rate limit per family.** At most 10 events of a family in any 10 minutes
  reach the sinks: a strict sliding window, so there's no burst after a quiet
  spell. Events beyond that are still written to the local log, marked
  `rate_limited`, and the next event that goes through is preceded by one
  `<family>.suppressed` summary saying how many were held back; the watcher sends
  that summary on its own if nothing else comes. Only metacomputer's own code can
  put an event outside this limit (`sink test`, the relay's `actions` events, the
  summaries); `events emit` and spool files can't.
- **Phones have their own cap.** Each ntfy sink gets at most 10 messages in any
  10 minutes and 30 in any hour, whatever they are: events, tests, summaries and
  sink-change announcements alike. It applies when sending: every attempt takes
  a slot, retries included, so a phone that was down doesn't get its whole
  backlog at once when it's back. What's waiting isn't dropped; it goes in order
  as slots free up (`events sink list` shows the wait). `events status` shows
  what the family limit is holding back.
- **Duplicates.** `notify` doesn't send the same message again within 5 minutes,
  and `events emit` drops the same event within 60 s.
- **Replacing or removing a sink** waits until nothing is sending its backlog
  (one lock per sink name), then switches to a new generation of it. A sender
  that still holds the old one sends nothing more to the old address; the old
  backlog is dropped (`--replace` says how many).
- **The rules are checked again when sending,** so a URL edited by hand into
  `events.json` that `sink add` would refuse (plain http off this Mac, a bad port)
  isn't used; the sink shows the error.
- **At least once.** An event can arrive twice (a timeout after the receiver
  had already stored it); drop duplicates by `id`.
- **Limits.** A backlog keeps at most 10,000 events (the oldest are dropped
  and counted). An event a receiver refuses outright (HTTP 400, 413, 415 or
  422) three times is dropped so the rest can move. OTLP sends up to 50 events
  per request, syslog up to 100 per connection, webhook and ntfy one.
- **OTLP partial success.** A collector that answers 2xx with
  `partialSuccess.rejectedLogRecords` above 0 stored only part of the batch:
  the rejected events are counted as dropped (not delivered), its
  `errorMessage` becomes the sink's `last_error`, and they aren't retried
  (OTLP says a client mustn't). `sink list` shows the count, and `events
  status` names the sink for a day. A sink-change announcement refused this
  way is given up and logged.
- **A wall-clock deadline per attempt.** Socket timeouts apply to each read,
  so a receiver that sends a byte a second could otherwise hold a connection
  open for as long as it liked. Each attempt runs with a deadline (3 s, or what
  is left of the sender's budget) and its connection is cut off when it
  passes, so the commands that deliver on their way (`start`, `stop`, `sink
  add|remove`, a pending announcement) end on time.

## The watcher

While at least one sink is configured, or a sink-change announcement is still
waiting to be delivered (removing the last sink mustn't stop its own
announcement's retries), `metacomputer start` starts a small
detached process (like the network gateway; never a LaunchAgent) and
`metacomputer stop` stops it. Its pid is in `$RUN_DIR/events-watcher.pid` and
its log is `$LOG_DIR/events-watcher.log`. Adding the first sink while the VM
runs starts it too. Every 30 seconds it:
- takes new files from the spool;
- retries backlogs whose backoff has passed;
- reads new lines of `$LOG_DIR/netgw-blocked.jsonl` (where it got to is kept in its state, so nothing is reported twice, and the log's earlier history is never reported) and sends what's due of the `blocked_connections` events.

On a thread of its own, whatever its cycle is doing (a health probe or a slow sink can take minutes),
it also looks at the gateway's log every 2 seconds (one `stat`; the file is read only when it grew). The first blocked connection after 5 minutes with none is sent from there at once,
so it reaches the sinks within a few seconds of the gateway refusing it. When the cycle itself happens to
read that first one, it's sent at once too, before the cycle's health probe and deliveries. Only that first one: the
follow-ups wait for their batch (at most one event a minute, slowing down for the same destinations),
the 5 quiet minutes are measured on this Mac's clock from when the watcher last saw a blocked
connection, and at most 6 such first reports go out in any hour (more wait like follow-ups). The family
limit and each phone's cap apply to them like to any other event, so a VM that floods or keeps going
quiet and starting again can't spam the sinks.

Its pidfile stays locked while it runs, so a second watcher for the same VM
exits at once; the pidfile and the log are private (0600). `stop` signals its
whole process group, so a health probe it started stops with it.

Every 5 minutes (the first 5 minutes after start) it runs a cheap health probe,
`metacomputer _health`: the VM's state, the gateway, SSH, the VM's own
`guest-status` (desktop session, Cua Driver's `doctor`, free disk) and whether
a desktop recording that should be running still is. No MCP connection, no GUI
work. It sends one `lifecycle.health` event when a condition starts and one
when it clears. With no sinks and nothing left to announce, it exits.

## The spool: events from other programs

Another program (the secrets vault, or your own scripts) can hand events to
the stream without calling metacomputer: it writes one JSON file per event
into the spool folder, `~/Library/Application Support/metacomputer/events/spool/`
(0700; `MCOMP_EVENTS_SPOOL` overrides). Every emission and every watcher cycle
takes those files, checks and cleans them, numbers them and delivers them like
any other event, then deletes them. Files that aren't valid events (bad JSON,
too deep, too big, a reserved type) go to `spool/.quarantine/` (the newest 50
are kept), and never hold up the rest. The spool is read through a pinned folder
descriptor, files are opened without following links or waiting (so a pipe or
link swapped in can't block or redirect it), checked on the open file and read
only up to the size limit. The contract is in
[architecture.md](../architecture.md#event-spool).

## Where things live

| What | Where |
| --- | --- |
| Sinks (kinds, URLs, headers, filters, signing keys) | `~/.config/metacomputer/events.json` (0600; folder 0700). `MCOMP_EVENTS_DIR` moves the whole folder. |
| Sequence numbers, each sink's backlog, count and delivery status per generation (`sinks/NAME.GENERATION.*`, a new generation each time a sink is added or replaced) and its lock (`sinks/NAME.lock`), debounce marks, the limits (`ratelimit.json`, `phonelimit.json`), pending sink-change announcements (`admin/`) and their outcomes (`admin.log`), the last configuration `events sink` wrote (`config.sha256`, `config.good.json`), the watcher's place in the gateway log | `~/.config/metacomputer/events-state/` (0700, files 0600). The next number is always one more than the highest anywhere (the counter, the log, the backlogs, what the sinks have), so a lost or restored counter doesn't go back. |
| The spool | `~/Library/Application Support/metacomputer/events/spool/` (`MCOMP_EVENTS_SPOOL`) |
| The local log of every event (except `actions`) | `$LOG_DIR/events.jsonl` (0600; `logs/` in the checkout by default); kept to 10 MB plus one older file |
| The watcher | `$RUN_DIR/events-watcher.pid`, `$LOG_DIR/events-watcher.log` |

There are no settings in `config/local.env` for this; the sinks are the whole
configuration.

## Privacy and where it ends

- **What leaves the Mac** is exactly what the table above lists, after
  cleaning, and only to sinks you added. That includes this Mac's short host
  name, the VM's name, and project folder paths. Leave out families you don't
  want sent.
- **The configuration is readable by anything that runs as you,** Claude
  included: its URLs, header values and signing keys. `sink list` and
  `sink show` never print header values and mask path parts that may be
  secret, but the file itself is plain JSON. So give sinks low-scope,
  ingest-only tokens that can't read anything back, and use a relay or a local
  Collector to keep stronger credentials elsewhere.
- **Sink changes need your terminal and are announced; hand edits are noticed and announced too,
  on a best-effort basis** (Commands, above). None of this holds against a program running as you
  (see "What it protects against").
- **Claude doesn't add or change sinks.** Those commands need your terminal,
  so when you ask, the skill has Claude give you the exact command to run there,
  with `--header-env` when a token is involved, so the token never passes
  through the chat.
- **Best effort, not an audit trail.** Anyone with your login can edit the
  backlog, the local log or the configuration, and a receiver that's down for
  long enough can miss events. For a record of every tool call that resists
  tampering, use the signed action log (`ACTION_LOG=on`, SECURITY.md); the
  secrets vault has its own audit.

## OCSF mapping for the security family

metacomputer doesn't emit [OCSF](https://schema.ocsf.io) itself. When your
SIEM wants it, map the `security` events in the Collector or SIEM like this:

| Event type | OCSF class | Activity | Mapping |
| --- | --- | --- | --- |
| `blocked_connections` | Network Activity (4001) | Refuse (5) | one record per entry of `data.destinations`: `dst_endpoint.ip` ← host, `dst_endpoint.port` ← port, `connection_info.protocol_name` ← proto, `disposition_id` 2 (Blocked), `count` ← count, `status_detail` ← reason, `src_endpoint.hostname` ← vm |
| `chrome_input_refused` | API Activity (6003) | Other (99) | `api.operation` ← `data.tool`, `status_id` 2 (Failure), `disposition_id` 2 (Blocked) |
| `actions_verify_failed` | Detection Finding (2004) | Create (1) | `finding_info.title` ← message, `finding_info.uid` ← id, `evidences` ← `data.files` |
| `untrusted_link` | Detection Finding (2004) | Create (1) | `finding_info.title` ← message, `finding_info.desc` ← `data.summary`, `url.hostname` ← host of `data.final_origin`, `observables` ← `data.origins` |
| `hidden_text` | Detection Finding (2004) | Create (1) | `finding_info.title` ← message, `finding_info.types` ← the keys of `data.signals` and `data.patterns`, `url.hostname` ← `data.host`, `count` ← `data.hidden` + `data.instruction_like` |

For every class: `time` ← `time` (as epoch milliseconds), `metadata.uid` ←
`id`, `metadata.product.name` = "metacomputer", `device.hostname` ← `host`,
`message` ← `message`, and `severity_id` from severity: info → 1
(Informational), warn → 3 (Medium), error → 4 (High).

## CloudEvents types

```
io.metavert.metacomputer.needs_you.{login,captcha,2fa,dialog,approval,other,in_use,refused,vault_locked,approval_waiting}
io.metavert.metacomputer.task.{finished,recording_stopped,recording_failed}
io.metavert.metacomputer.security.{blocked_connections,chrome_input_refused,actions_verify_failed,untrusted_link,
                                   sink_added,sink_removed,sink_config_changed,sink_changes,
                                   lesson_approved,lesson_edited,lesson_removed,lessons_resealed,hidden_text}
io.metavert.metacomputer.<family>.suppressed   (the rate limit's summary, in any family)
io.metavert.metacomputer.lifecycle.{started,stopped,reset,restored,checkpoint,baseline,build_done,build_failed,
                                    controller_connected,controller_released,health,test,storage_attached,
                                    handoff_opened,handoff_done,handoff_cancelled,handoff_timed_out}
io.metavert.metacomputer.actions.{start,call,result,refused,checkpoint,end,sign_failed}
```

Spool producers may add types of their own (lowercase letters, digits and
`_`), in these five families.
