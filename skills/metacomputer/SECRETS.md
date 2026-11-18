# Secrets: `metacomputer secrets`

The user keeps passwords, one-time-code seeds, saved browser logins and API keys in a vault on the Mac. You use them in the VM without the values passing through the conversation. Background: SECURITY.md, "Secrets and logins", in the project folder (`metacomputer where`).

## Who runs what

- **Only the user, in their own terminal window:** `init`, `set`, `delete`, `bind`, `unlock`, `rekey`, `recover`, `rotate-session-key`, `session policy`, `session delete`, `audit-sink add|remove|show --key`. These ask for the vault passphrase or a value.
  - Never run them, never try, and never ask for the passphrase, a password, a code or a key in chat. Give the user the exact command to run in their own terminal (not a `!` command here: its output lands in this conversation).
  - If the user offers a value in chat, ask them to keep it out and store it themselves: `metacomputer secrets set NAME --kind password --origin https://site --username U` (`--kind totp` for a 2FA seed, `--kind api --api-host api.example.com` for an API key).
- **You:** `list`, `fill`, `session save|load`, `token`, `tokens`, `revoke`, `status`, `audit`, `audit-sink list|test`.

## First: what's stored

`metacomputer secrets list` shows names, kinds, sites (or API hosts), usernames, which `--field`s each secret fills, its approval, any project limit and its age. Never values. It works only while the vault is unlocked.

## Exit codes (fill, session, token) and what to tell the user

| Exit | Means | Do |
| --- | --- | --- |
| 0 | done | carry on; for a fill, `chars` is the check |
| 2 | refused; the `reason` says why | wrong site or field: fix it (focus the right field) and try once more. "not allowed at the vault's terminal": the user said no or didn't answer; ask them. "limited to Claude Code sessions in ...": this project may not use it; tell the user. A required audit sink or a multiplexer: pass the reason on. Never work around a refusal |
| 3 | the vault is locked | ask the user to run `metacomputer secrets unlock` in their own terminal, then try again. No vault yet: they run `metacomputer secrets init` and `set` |
| 4 | error | VM off, no Chrome, no tab on that site. "Rebuild the VM": the VM predates the fill or proxy account; tell the user. "Bind again": the user runs `metacomputer secrets bind` |

## When the user has to approve

A secret set to `each`, a vault unlocked with `--confirm each`, every sealed-session save (and load, unless relaxed) and an `each` API token wait up to 2 minutes for the user. Their unlock terminal shows the question and "Asked by Claude Code in <this project>". Tell them which secret, what for, and that they type in the vault's terminal:
- `NAME` to allow this use;
- `NAME session` for the rest of this Claude Code session;
- `NAME unlock` until the vault locks.

Inside tmux or screen these are refused unless the user unlocked with `--allow-multiplexer`; the reason says so.

## Signing in to a site (fill)

Fills work only in page-mode Chrome (`browser_prepare`), not Firefox or human-mode Chrome.

1. Open the sign-in page in page mode (`browser_prepare`, `browser_navigate`).
2. Focus the field with a trusted click: `browser_click {ref, delivery_mode: "foreground"}`. A `dom_event` click may not focus it.
   - Or let the fill focus it: add `--selector '#username'` (an id or name from a `dom_refs_v1` snapshot). It must match exactly one element. A selector searches the page itself, not frames or shadow roots; for a field in a frame, click into it.
3. `metacomputer secrets fill NAME --field username`.
4. Focus the password field, then `metacomputer secrets fill NAME --field password`.
5. For a 2FA code, focus the code field, then `metacomputer secrets fill CODE_NAME --field otp` (the vault works out the current code).
6. With a named profile, add `--profile P` to each fill.
7. Submit with a click, then check the signed-in page with a fresh snapshot.

`{"ok":true,...,"chars":N}` means it was typed and checked. That is the verification: don't read the field back.

## Keeping the user signed in (sealed sessions)

1. Use a named profile (`isolated_named`, for example `x`).
2. Sign in: fill as above, or let the user sign in once through `metacomputer view`.
3. End the browser session (`end_session`): a profile Chrome is using can't be saved.
4. `metacomputer secrets session save NAME --profile x`. The user confirms it in their terminal.
5. Later, with no Chrome on that profile: `metacomputer secrets session load NAME`, then `browser_prepare` with `{mode: "isolated_named", name: "x"}`. A session loads only under the profile name it was saved from.

## Using an API key from the VM (tokens)

The key never goes into the VM. A program in the VM calls the API through the vault's proxy with a short-lived token instead.

1. `metacomputer secrets list` shows `api` secrets and their hosts.
2. `metacomputer secrets token NAME [--ttl 15m]` prints JSON with `token`, `token_id`, `base_url` (`http://127.0.0.1:18765/NAME/` inside the VM), `header` and `expires`.
3. In the VM, send requests to `base_url` + the API's path, with the token where the key would go:
   - `curl -H "Authorization: Bearer <token>" http://127.0.0.1:18765/NAME/v1/...` (or the secret's own header, such as `x-api-key: <token>`; `X-Metacomputer-Token: <token>` always works);
   - an SDK: set its base URL to `http://127.0.0.1:18765/NAME` (plus the version path it expects) and its API key to the token.
4. Answers: 401 the token expired or was revoked (get a new one); 403 a path outside the secret's prefix; 502 the API redirected elsewhere or couldn't be reached; 503 the user's required audit sink is down (tell them).
5. `metacomputer secrets revoke <token_id>` when done; `tokens` lists the live ones. Tokens also end when the vault locks.

Never put a real key in the VM (no file, environment variable or command line there), and never ask the user to paste one. If an answer shows `[key withheld by metacomputer secrets]`, the API echoed the key and the proxy blanked it.

## Never

- Read a secret, or a filled field's value: no snapshot that includes a field's value, no "show password" buttons, no Chrome developer tools (F12, `devtools://`), no `javascript:` addresses, no querying or posing as Chrome's DevTools through `metacomputer sh` or `metacomputer ssh ops`. If a page shows a secret anyway, don't read it out, and tell the user.
- Type a secret with `type_text`, `browser_type` or a shell command.
- Record sign-in steps unless the user wants that: password fields show dots, but usernames and codes are visible on screen.
