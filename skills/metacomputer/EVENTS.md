# Getting the user's attention: events

The user can have metacomputer send events to their phone (ntfy), a chat tool, a webhook, an OpenTelemetry Collector or a SIEM. `~/.local/bin/metacomputer events status` says whether any sink is set up; "off" means none, and nothing is sent anywhere. The project's `docs/events.md` (`metacomputer where`) has the details for people.

What the stream is for: getting the user's attention, and telling them about sink changes. It cleans hostile text from the VM and web pages and limits floods, but it doesn't protect against a program running as the user, which can read the sink settings and post to them directly. Don't describe it to the user as more than that.

## When you're blocked on the user: `metacomputer notify`

Run it the moment you stop to wait for a person, every time. For a CAPTCHA or bot check, a sign-in the user does themselves or a 2FA prompt, use `metacomputer handoff` instead (next section): it sends this notice for you and pauses your actions until they're done.

```
~/.local/bin/metacomputer notify "Approval needed to send the order at example.com" --why approval --action "Run metacomputer view --open (the live-view password goes on your clipboard: paste with Cmd+V), check the order, then tell Claude to carry on"
```

| You hit | `--why` |
| --- | --- |
| A sign-in page, an expired session, "create an account" | `login` (with `handoff` when the user signs in) |
| A CAPTCHA or an "are you human" check | `captcha` (always with `handoff`) |
| A one-time code, 2FA, "approve on your phone" | `2fa` (with `handoff` when the vault can't fill it) |
| A dialog you won't accept yourself: permissions, terms, payment, a destructive confirmation, a download or security warning | `dialog` |
| A decision only the user can make: a purchase, sending a message, deleting something that matters | `approval` |
| The computer is in use by another session (`metacomputer who` shows it ACTIVE) and the user isn't answering; anything else | `other` |

- **The message:** one line saying what you need, where, and why. Write "Sign-in needed at example.com to download the March report", not "I'm stuck".
- **`--action`:** what the user should do, short and concrete. When it's in the live view, give the exact command and say where the password is. For example:
  - "Run metacomputer view --open (the live-view password goes on your clipboard: paste with Cmd+V), accept the dialog, then tell Claude to carry on";
  - "Run metacomputer secrets unlock in your own terminal".
- **Never put a secret in either.** That means no passwords, codes, tokens, cookies, personal data from the page, or full links. Name the site, not the URL: queries are cut off anyway, but don't rely on that.
- **Say it in your reply too.** `notify` adds to the chat; it doesn't replace it. Its output says which sinks took it. "Nobody was pinged" means no sink is set up. On a phone it starts with "[from Claude]", so the user knows who asked.
- **Then wait sensibly.**
  - In a conversation, stop and ask. When the user says go on, read the screen again: it changed while you waited, and element tokens from before are stale.
  - In an unattended run, do the parts of the task that don't depend on the blocked step, or end the run with a clear report of where you stopped. If you check back, wait minutes between checks, not seconds.
  - Never work around the block: no other sign-in route, no CAPTCHA services, no other browser or profile to get past a check, no clicking through a dialog you wouldn't accept.
- **Once per block.** Don't send the same notify again while you wait (the same text within 5 minutes isn't sent anyway). Send a new one only when something changes, for example the sign-in worked and a 2FA prompt followed.
- **Limits.** At most 10 events of a family reach the sinks in any 10 minutes (the rest stay in the local log and are summed up later), and a phone gets at most 10 messages in any 10 minutes and 30 in an hour, whatever they are; those wait their turn and arrive late rather than not at all. Another reason to notify only when it matters.

## When the step is the user's: `metacomputer handoff`

[SKILL.md](SKILL.md) has the steps. For events, what matters:
- `handoff` sends the `needs_you` notice itself (as `notify` does: your `--say` text, cleaned, with the live-view command as its action and the same 5-minute repeat rule), plus `lifecycle.handoff_opened`, and later `handoff_done`, `handoff_cancelled` or `handoff_timed_out`. Don't `notify` about the same step.
- `--say`: one line, what to do and where ("Tick the 'Verify you are human' box on example.com in Chrome"). No secrets, codes or full links. The user's `handoff done --note` comes back to you, never into an event.
- In an unattended run, pick a `--timeout` that fits (15 minutes by default, at most 2 hours). At the limit it returns 124 and your actions are no longer paused: carry on with what doesn't depend on that step, or end the run with a clear report.

## Sent automatically: don't duplicate

metacomputer sends these itself, so don't call `notify` for them:
- **The bridge refused a call because another controller is using the computer** (its error says "in use by another controller"). `needs_you.in_use` went out. Ask in chat.
- **Bounded mode keeps refusing the same call.** `needs_you.refused` goes out once it repeats. Tell the user what was refused, as the Rules in SKILL.md say.
- **`metacomputer secrets fill`, `secrets session save|load` or `secrets token` exits 3** (the vault isn't unlocked, or doesn't exist yet). `needs_you.vault_locked` went out. Ask in chat for the unlock (or `secrets init`).
- **A handoff and how it ended** (`metacomputer handoff`): its `needs_you` notice and the `lifecycle.handoff_*` events.
- **The secrets vault waiting for the user's typed confirmation.** The vault sends its own event (`needs_you.approval_waiting`). Tell the user in chat that a confirmation is waiting.
- **Finished work:** `metacomputer task finish` (the task, how long it took, its videos and files) and `metacomputer record stop` (the recording saved, or failed). When the user asked to hear when it's done, finishing the task is the message. Only if the work had no task folder, send `~/.local/bin/metacomputer events emit task finished --message "what is done and where the results are"`. Don't use `notify` for that: it's for being blocked.
- **Security signals:** connections from the VM that the gateway blocked, the Chrome guard's refusals, a failed `actions verify`, and what `web open-untrusted` found (password prompts, downloads, a redirect to another site).
- **Lifecycle and health:** start, stop, reset, restore, checkpoints, the baseline, builds, sessions taking or releasing the computer, and the watcher's health probe (VM down, gateway or SSH down, desktop not ready, Cua Driver's doctor, low disk, a recording that died).

## Sinks: only when the user asks

- **Reading is fine any time:** `metacomputer events status`, `events sink list`, `events tail`. They never show header values.
- **You can't change sinks, and must not try.** `events sink add` and `remove` decide what leaves the Mac, so they work only from the user's own terminal and refuse without one. Never get around that (no `script`, no pseudo-terminals, no editing `events.json`).
- **When the user asks** (for example "send my events to ntfy" or "set up Datadog"):
  1. Give them the exact command to run in their own terminal, and say which families it sends.
  2. Every change is announced to their existing sinks as `security.sink_added` or `sink_removed` (and an edit to the file by any other means, when noticed, as `security.sink_config_changed`), so tell them to expect that. If the command says the sinks changed while they were answering, they just run it again.
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
- **Don't fake metacomputer's own events.** `metacomputer events emit` is for the user's scripts. Its events are marked as sent from the command line, and it refuses the types metacomputer itself sends (blocked connections, health, sink changes and so on). Use `notify` when you're blocked; let the rest come from where it comes.
