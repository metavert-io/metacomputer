# Getting the user's attention: events

The user can have metacomputer send events to their phone (ntfy), a chat tool, a webhook, an OpenTelemetry Collector or a SIEM. `~/.local/bin/metacomputer events status` says whether any sink is set up; "off" means none, and nothing is sent anywhere. The project's `docs/events.md` (`metacomputer where`) has the details for people.

## When you're blocked on the user: `metacomputer notify`

Run it the moment you stop to wait for a person, every time:

```
~/.local/bin/metacomputer notify "Sign-in needed at example.com to download the March report" --why login --action "Sign in through the live view (metacomputer view), then tell Claude to carry on"
```

| You hit | `--why` |
| --- | --- |
| A sign-in page, an expired session, "create an account" | `login` |
| A CAPTCHA or an "are you human" check | `captcha` |
| A one-time code, 2FA, "approve on your phone" | `2fa` |
| A dialog you won't accept yourself: permissions, terms, payment, a destructive confirmation, a download or security warning | `dialog` |
| A decision only the user can make: a purchase, sending a message, deleting something that matters | `approval` |
| The computer is in use by another session (`metacomputer who` shows it ACTIVE) and the user isn't answering; anything else | `other` |

- **The message:** one line saying what you need, where, and why. Write "Sign-in needed at example.com to download the March report", not "I'm stuck".
- **`--action`:** what the user should do, short and concrete. For example:
  - "Sign in through the live view (metacomputer view), then tell Claude to carry on";
  - "Solve the CAPTCHA in the live view";
  - "Run metacomputer secrets unlock in your own terminal".
- **Never put a secret in either.** That means no passwords, codes, tokens, cookies, personal data from the page, or full links. Name the site, not the URL: queries are cut off anyway, but don't rely on that.
- **Say it in your reply too.** `notify` adds to the chat; it doesn't replace it. Its output says which sinks took it. "Nobody was pinged" means no sink is set up.
- **Then wait sensibly.**
  - In a conversation, stop and ask. When the user says go on, read the screen again: it changed while you waited, and element tokens from before are stale.
  - In an unattended run, do the parts of the task that don't depend on the blocked step, or end the run with a clear report of where you stopped. If you check back, wait minutes between checks, not seconds.
  - Never work around the block: no other sign-in route, no CAPTCHA services, no clicking through a dialog you wouldn't accept.
- **Once per block.** Don't send the same notify again while you wait (the same text within 5 minutes isn't sent anyway). Send a new one only when something changes, for example the sign-in worked and a 2FA prompt followed.
- **Limits.** At most 10 events of a family reach the sinks per 10 minutes; the rest wait in the local log and are summed up later. Another reason to notify only when it matters.

## Sent automatically: don't duplicate

metacomputer sends these itself, so don't call `notify` for them:
- **The bridge refused a call because another controller is using the computer** (its error says "in use by another controller"). `needs_you.in_use` went out. Ask in chat.
- **Bounded mode keeps refusing the same call.** `needs_you.refused` goes out once it repeats. Tell the user what was refused, as the Rules in SKILL.md say.
- **`metacomputer secrets fill` or `secrets session save|load` exits 3** (the vault isn't unlocked, or doesn't exist yet). `needs_you.vault_locked` went out. Ask in chat for the unlock (or `secrets init`).
- **The secrets vault waiting for the user's typed confirmation.** The vault sends its own event (`needs_you.approval_waiting`). Tell the user in chat that a confirmation is waiting.
- **Finished work:** `metacomputer task finish` (the task, how long it took, its videos and files) and `metacomputer record stop` (the recording saved, or failed). When the user asked to hear when it's done, finishing the task is the message. Only if the work had no task folder, send `~/.local/bin/metacomputer events emit task finished --message "what is done and where the results are"`. Don't use `notify` for that: it's for being blocked.
- **Security signals:** connections from the VM that the gateway blocked, the Chrome guard's refusals, a failed `actions verify`, and what `web open-untrusted` found (password prompts, downloads, a redirect to another site).
- **Lifecycle and health:** start, stop, reset, restore, checkpoints, the baseline, builds, sessions taking or releasing the computer, and the watcher's health probe (VM down, gateway or SSH down, desktop not ready, Cua Driver's doctor, low disk, a recording that died).

## Sinks: only when the user asks

- **Reading is fine any time:** `metacomputer events status`, `events sink list`, `events tail`. They never show header values.
- **You can't change sinks, and must not try.** `events sink add` and `remove` decide what leaves the Mac, so they work only from the user's own terminal and refuse without one. Never get around that (no `script`, no pseudo-terminals, no editing `events.json`).
- **When the user asks** (for example "send my events to ntfy" or "set up Datadog"):
  1. Give them the exact command to run in their own terminal, and say which families it sends.
  2. Every change is announced to their existing sinks as `security.sink_added` or `sink_removed`, so tell them to expect that.
  3. Afterwards you may run `metacomputer events sink test NAME` and report the result.
- **Tokens:**
  - Never put a token from the chat into a command.
  - When a sink needs one (an Authorization header, an API key), use `--header-env` in the command you give them, so the token stays out of shell history: `metacomputer events sink add phone --kind ntfy --url https://ntfy.sh/TOPIC --header-env 'Authorization=Bearer $NTFY_TOKEN' --families needs_you,task`.
  - If they pasted a token into the chat anyway, say it's now in the transcript and suggest replacing it. Don't use it.
- **Recommend ingest-only, low-scope tokens.** The sink file is readable by anything running as the user, you included.
- **Which kind:**
  - their phone: `ntfy`;
  - Slack, Discord, Mattermost or Teams: `webhook`, to a small relay (`docs/events.md` has one);
  - Datadog, Splunk, CloudWatch, Cloud Logging, Loki or S3: `otlp`, to an OpenTelemetry Collector on the Mac;
  - a SIEM that takes syslog: `syslog`, over TLS.
- **Hands off the files.** Never edit `~/.config/metacomputer/events.json` or its `events-state` folder. Never remove a sink to quiet things down unless the user asks.
