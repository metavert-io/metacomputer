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

## Quick start

```bash
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

When Claude is blocked on you (a sign-in, a CAPTCHA, a 2FA code, a dialog it
won't accept, an approval), the `metacomputer` skill tells it to run
`metacomputer notify "…" --why login --action "…"`. That reaches every sink
that takes the `needs_you` family.

## Commands

| Command | What it does |
| --- | --- |
| `events sink add NAME --kind webhook\|otlp\|syslog\|ntfy --url URL [--header 'Name: value' ...] [--header-env 'Name=VAR' ...] [--ca FILE] [--families F1,F2\|all] [--min-severity info\|warn\|error] [--replace]` | Add a sink. A webhook sink prints its signing key once. |
| `events sink list [--json]` | Every sink: kind, where (with secret-looking path parts masked), filters, backlog, last delivery or error. Header values are never shown. |
| `events sink show NAME [--key] [--json]` | One sink; `--key` also prints a webhook sink's signing key. |
| `events sink test NAME` | Send a test event to that sink now (ignoring its backoff) and say whether it was accepted. |
| `events sink remove NAME` | Remove the sink and its backlog. |
| `events tail [N] [--json]` | The newest events in the local log. |
| `events emit FAMILY TYPE --message TEXT [--severity S] [--action TEXT] [--data JSON] [--field K=V ...]` | Emit an event from a script. |
| `events status [--json\|--line]` | Sinks, watcher, total backlog, failing sinks. |
| `events drain [--now]` | Take the spool and retry every backlog (what the watcher does every 30 s); `--now` ignores the backoff. |
| `events watcher start\|stop\|status` | The watcher by hand (`start` and `stop` do it for you). |
| `notify "TEXT" [--why login\|captcha\|2fa\|dialog\|approval\|other] [--action "what to do"]` | A `needs_you` event: Claude is blocked on a human. |

Header values usually hold ingest tokens. `--header 'Authorization: Bearer …'`
puts the token in your shell history; `--header-env` takes it from the
environment instead, for example with a secret store that runs a command with
the value set:
`--header-env 'Authorization=Bearer $NTFY_TOKEN'`.

## Families and what they emit

`--families all` (the default) means `needs_you,task,security,lifecycle`.
`actions` is never part of `all`; a sink gets it only when you name it.

| Family | Type | Severity | When | Data |
| --- | --- | --- | --- | --- |
| `needs_you` | `login`, `captcha`, `2fa`, `dialog`, `approval`, `other` | warn | Claude ran `metacomputer notify --why …` | `why`, `declared_by: claude`; the event's `action` says what to do |
| `needs_you` | `in_use` | warn | Claude asked for the VM while another controller had it (the bridge refused the call); once per session per 5 min | the tool, the other controllers (project folder, idle time, last tool) |
| `needs_you` | `refused` | warn | Cua Driver's bounded mode refused the same call twice within 5 min (once per 5 min after that) | tool, the driver's reason, count |
| `needs_you` | `vault_locked` | warn | `metacomputer secrets fill` or `secrets session save\|load` exited 3, "vault locked" (an exit hook in `bin/metacomputer`; it takes effect where the secrets vault is installed) | the action says to run `metacomputer secrets unlock` |
| `needs_you` | `approval_waiting` (and others) | warn | The secrets vault drops these into the spool | whatever the vault puts in `data` |
| `task` | `finished` | info; error when nothing could be copied | `metacomputer task finish` | task, duration, videos (path, seconds), contact sheets, file and screenshot counts, the results folder |
| `task` | `recording_stopped` | info | `metacomputer record stop` saved a recording | name, duration, path, description file, key-moment count, audio yes/no, mode, size |
| `task` | `recording_failed` | error | `record stop` couldn't save it | name, mode; the message says why |
| `security` | `blocked_connections` | warn | New lines in the gateway's `netgw-blocked.jsonl`, batched: at most one event a minute | count; per destination: host, port, protocol, reason, count, first and last time. Never payloads. |
| `security` | `chrome_input_refused` | warn | The bridge's Chrome guard refused `set_value` or `run_script` aimed at Chrome; once per session and tool per 5 min | tool (never the value) |
| `security` | `actions_verify_failed` | error | `metacomputer actions verify` found a changed log | files that failed, how many were checked |
| `security` | `untrusted_link` | warn | `web open-untrusted` found a password field, a download, or that the link ended on another site | summary, start and final origin (scheme, host, port only), password fields, download count and names, origins passed through |
| `lifecycle` | `started`, `stopped`, `reset`, `restored`, `checkpoint`, `baseline`, `build_done`, `build_failed` | info (build_failed: error) | those commands | checkpoint name, what it was restored from |
| `lifecycle` | `controller_connected`, `controller_released` | info | A Claude Code session's first acting call; the session ending; `metacomputer release` | bridge and Claude Code process ids |
| `lifecycle` | `health` | warn or error; info when it clears | The watcher's probe every 5 min, one event per change | `condition`: `vm_off`, `gateway`, `ssh`, `not_ready`, `doctor`, `disk_low` (under 3 GB), `recording_failed`; `state`: `problem` or `ok` |
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
| `vm` | The VM's name (`VM_NAME`). |
| `project` | The folder of the Claude Code session that caused it, when known (its working folder); else the command's current folder. In real events this is the full path. |
| `message` | One line for a person. |
| `action` | What the person should do, mostly for `needs_you`; otherwise null. |
| `data` | Small, cleaned details (below). |
| `host` | This Mac's short host name. |

### What is cleaned out

Before anything is logged or sent:
- control characters, terminal escape sequences and bidirectional-text overrides are removed from every string;
- URLs lose their query string, fragment and any `user:password@`, everywhere (`https://site.example/a?token=x` becomes `https://site.example/a`);
- keys in `data` that look secret are dropped: password, passphrase, secret, token, cookie, header(s), credential, api key, private or session key, auth/authorization, and the exact keys text, value, values, script, typed, otp, totp, code, pin, vnc…;
- any configured sink's header values, and the live-view password in `$SSH_DIR/vnc-pass`, are replaced by `[redacted]` wherever they appear;
- sizes are capped: message 500 characters, action 300, each string in `data` 300, 50 keys or list items, 4 levels deep, `data` 4 KB in all.

Typed text, passwords, tokens and page contents are never part of an event in
the first place: the hooks pass tool names, counts, origins and paths.

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

Severity: info = 9 `INFO`, warn = 13 `WARN`, error = 17 `ERROR`.
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
- Structured data `metacomputer@32473` with `seq`, `family`, `vm`, `id`.
  32473 is the enterprise number reserved for documentation (RFC 5612).
- MSG is the event as JSON, without a byte-order mark.

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

`https://` only, except to this Mac's loopback. On ntfy.sh anyone who knows a
topic can read it, so use a long random topic, or your own server or account
with an access token (`--header-env 'Authorization=Bearer $NTFY_TOKEN'`).

## Filtering

Each sink has families (`--families needs_you,task`, `all`, or `all,actions`)
and a minimum severity (`--min-severity warn` drops info). An event goes to
every sink whose filters it passes.

## Delivery

- **At the moment it happens.** The command or bridge that emits the event
  tries each matching sink right away, in parallel, with a 3-second timeout.
  The bridges (`mcp-lazy`, the action-log relay) do this on a background
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
- **At least once.** An event can arrive twice (a timeout after the receiver
  had already stored it); drop duplicates by `id`.
- **Limits.** A backlog keeps at most 10,000 events (the oldest are dropped
  and counted). An event a receiver refuses outright (HTTP 400, 413, 415 or
  422) three times is dropped so the rest can move. OTLP sends up to 50 events
  per request, syslog up to 100 per connection, webhook and ntfy one.

## The watcher

While at least one sink is configured, `metacomputer start` starts a small
detached process (like the network gateway; never a LaunchAgent) and
`metacomputer stop` stops it. Its pid is in `$RUN_DIR/events-watcher.pid` and
its log is `$LOG_DIR/events-watcher.log`. Adding the first sink while the VM
runs starts it too. Every 30 seconds it:
- takes new files from the spool;
- retries backlogs whose backoff has passed;
- reads new lines of `$LOG_DIR/netgw-blocked.jsonl` (where it got to is kept in its state, so nothing is reported twice, and the log's earlier history is never reported) and sends at most one `blocked_connections` event a minute.

Every 5 minutes (the first 5 minutes after start) it runs a cheap health probe,
`metacomputer _health`: the VM's state, the gateway, SSH, the VM's own
`guest-status` (desktop session, Cua Driver's `doctor`, free disk) and whether
a desktop recording that should be running still is. No MCP connection, no GUI
work. It sends one `lifecycle.health` event when a condition starts and one
when it clears. With no sinks left, it exits.

## The spool: events from other programs

Another program (the secrets vault, or your own scripts) can hand events to
the stream without calling metacomputer: it writes one JSON file per event
into the spool folder, `~/Library/Application Support/metacomputer/events/spool/`
(0700; `MCOMP_EVENTS_SPOOL` overrides). Every emission and every watcher cycle
takes those files, checks and cleans them, numbers them and delivers them like
any other event, then deletes them. The contract is in
[architecture.md](../architecture.md#event-spool).

## Where things live

| What | Where |
| --- | --- |
| Sinks (kinds, URLs, headers, filters, signing keys) | `~/.config/metacomputer/events.json` (0600; folder 0700). `MCOMP_EVENTS_DIR` moves the whole folder. |
| Sequence numbers, backlogs, delivery status, debounce marks, the watcher's place in the gateway log | `~/.config/metacomputer/events-state/` (0700, files 0600) |
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
- **Claude doesn't add or change sinks on its own.** The skill tells it to do
  that only when you ask, and to have you run `sink add` yourself when a token
  is involved, so the token never passes through the chat.
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

For every class: `time` ← `time` (as epoch milliseconds), `metadata.uid` ←
`id`, `metadata.product.name` = "metacomputer", `device.hostname` ← `host`,
`message` ← `message`, and `severity_id` from severity: info → 1
(Informational), warn → 3 (Medium), error → 4 (High).

## CloudEvents types

```
io.metavert.metacomputer.needs_you.{login,captcha,2fa,dialog,approval,other,in_use,refused,vault_locked,approval_waiting}
io.metavert.metacomputer.task.{finished,recording_stopped,recording_failed}
io.metavert.metacomputer.security.{blocked_connections,chrome_input_refused,actions_verify_failed,untrusted_link}
io.metavert.metacomputer.lifecycle.{started,stopped,reset,restored,checkpoint,baseline,build_done,build_failed,
                                    controller_connected,controller_released,health,test}
io.metavert.metacomputer.actions.{start,call,result,refused,checkpoint,end,sign_failed}
```

Spool producers may add types of their own (lowercase letters, digits and
`_`), in these five families.
