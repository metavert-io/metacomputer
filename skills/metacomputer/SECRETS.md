# Secrets: `metacomputer secrets`

The user keeps passwords, one-time-code seeds, saved browser logins and API keys in a vault on the Mac. You use them in the VM without the values passing through the conversation. Background: SECURITY.md, "Secrets and logins", in the project folder (`metacomputer where`).

## Who runs what

- **Only the user, in their own terminal window:** `init`, `set`, `delete`, `policy`, `bind`, `unlock`, `rekey`, `recover`, `rotate-session-key`, `session policy`, `session delete`, `import-passwords`, `import-session`, `audit-sink add|remove|show --key`. These ask for the vault passphrase or a value (the imports also show macOS's question about Chrome's key).
  - To bring saved logins in, suggest: `metacomputer secrets import-passwords --browser chrome` (or `--csv FILE` for a Chrome, Passwords app, 1Password or Bitwarden export; `--dry-run` first shows what would come in). To bring a signed-in session for one site from their Chrome: `metacomputer secrets import-session --browser chrome --site SITE --name NAME --to-profile PROFILE` (cookies only; localStorage doesn't come along). Then load it like any sealed session (below).
  - Never run them, never try, and never ask for the passphrase, a password, a code or a key in chat. Give the user the exact command to run in their own terminal (not a `!` command here: its output lands in this conversation).
  - If the user offers a value in chat, ask them to keep it out and store it themselves: `metacomputer secrets set NAME --kind password --origin https://site --username U` (`--kind totp` for a 2FA seed, `--kind api --api-host api.example.com` for an API key).
- **Only the user, from any terminal:** `approve ID`, `rule add` and `presence on|off|auto`. They show macOS's Touch ID / password dialog (or ask for the passphrase): never run them yourself, even though you could make the dialog appear. Tell the user the command.
- **You:** `list`, `fill`, `login`, `session save|load`, `token`, `tokens`, `revoke`, `requests`, `deny ID` (to withdraw a request of yours), `rule list`, `status`, `audit`, `audit-sink list|test`, `settle`, and the MCP tools `request_site_login` and `teleport_browser_session`. `lock` only if the user asks you to lock the vault.

## First: what's stored

`metacomputer secrets list` shows names, kinds, sites (or API hosts), usernames, which `--field`s each secret fills, its approval, any project limit and its age. Never values. It works only while the vault is unlocked.

## Exit codes (fill, login, session, token) and what to tell the user

| Exit | Means | Do |
| --- | --- | --- |
| 0 | done | carry on; for a fill, `chars` is the check |
| 2 | refused; the `reason` says why | wrong site or field: fix it (focus the right field) and try once more. "not allowed at the vault's terminal", "refused with `deny`", "refused at the Touch ID dialog": the user said no or didn't answer; ask them. "limited to Claude Code sessions in ...": this project may not use it; tell the user. A required audit sink or a multiplexer: pass the reason on. Never work around a refusal |
| 3 | the vault is locked | ask the user to run `metacomputer secrets unlock` in their own terminal, then try again. No vault yet: they run `metacomputer secrets init` and `set` |
| 4 | error | VM off, no Chrome, no tab on that site. "Rebuild the VM": the VM predates the fill or proxy account; tell the user. "Bind again": the user runs `metacomputer secrets bind` |
| 5 | sent, but the vault never answered | it may still be finishing. Run `metacomputer secrets settle` (it returns once the vault is done) before trying again or doing anything else on that page |

A fill or sign-in you interrupt (Ctrl-C, a timeout that kills the command) is withdrawn: the vault stops it before its next step and answers with a "stopped" reason. Don't retry it in a loop.

## When the user has to approve

A secret set to `each`, a vault unlocked with `--confirm each`, every sealed-session save, every load of a whole browser profile, an `each` API token and every use on an identity provider's sign-in page (Google, Microsoft, Apple, Okta) wait up to 2 minutes for the user. Their unlock terminal shows the question, a request id and "Asked by Claude Code in <this project>"; `metacomputer secrets requests` lists what waits. Tell them which secret, what for, and how to answer:
- in the vault's terminal: `NAME` to allow this use, `NAME session` for the rest of this Claude Code session, `NAME unlock` until the vault locks;
- or from any terminal: `metacomputer secrets approve ID` (Touch ID or their login password; `--passphrase` for the vault passphrase), for this use only; `--scope session` or `--scope unlock` reaches further, and the dialog or the prompt names it.

In presence mode (the default on a Mac with Touch ID; `metacomputer secrets status` says which) a typed answer only chooses `NAME` / `NAME session` / `NAME unlock`, and the user then confirms with Touch ID or the passphrase: tell them to expect that.

On an identity provider's page, and for a whole-profile session load, every use asks and `session` / `unlock` cover only that use: don't suggest them there. Inside tmux or screen typed answers are refused unless the user unlocked with `--allow-multiplexer` or `--touch-id`; the reason says so. If the user set up an unattended rule for this project (`rule list`), covered uses don't ask. A rule or an earlier answer never changes where a value goes: each fill is still checked against the focused page's site.

## Signing in: the MCP tool or the command

**`request_site_login {url}`** (the `metacomputer` server's own tool, in Cua Spaces' shape) does a standard sign-in in one step. Use it when the vault has a login for that exact site (`list`) and the page has an ordinary form: a username and a password field on the page itself, or a username-only first step.
- It opens page-mode Chrome on `url`, or uses your tab: pass `session`, `target_id`, `tab_id` and `pid` (the Chrome process you bound it with in `get_browser_state`); the tab must be in that Chrome.
- It finds the fields, has the vault fill them under one approval, and submits (`submit: false` to submit yourself). Pass `username` when the site has several logins, and `agent` (a short name for yourself) if you like: the user sees it marked unverified.
- If the user must approve, it answers `status: "pending"` with a `request_id`. Tell them the id and how to approve (below), then call again with `{url, request_id, wait_secs: 60}`.
- `status: "filled"` gives the driver `session` and `same_site` (whether the page is still on the login's site). No address of the page and no tab id comes back: find the tab with `list_windows` and `get_browser_state` (in that session), then read the page itself with a snapshot. `denied` means the user said no; don't retry.
- The fill refuses when another tab in that Chrome is on the same site: close it. A multi-step sign-in needs one call per page.
- Through this tool the vault sees the request as the hub's, not your session's, so a login limited to your project, an unattended rule or a `NAME session` answer doesn't cover it: it asks (or is refused). Use `secrets login` when the user set those up for this project.

**Use the commands below instead** for:
- a field inside a frame (the tool reads the page itself only);
- a 2FA code (`fill --field otp`) or a `text` secret;
- a form the tool can't read (custom widgets), or a page where you must control each step;
- uses the user covered for this project with `--project`, a rule or a `session` answer.

**`teleport_browser_session {sites: ["github.com"]}`** loads a session the user imported from their own Chrome, with the same two steps (`consent_required`, then `moved: true` with the profile to open with `browser_prepare`). Only the cookies those sites get are loaded. If there's none, its error names the `import-session` command for the user. For a profile you saved from the VM yourself, use `session load` (below).

Either way the VM may hold a value from then on, so until the VM is reset the action log and the hub's logs keep nothing the VM says (SECURITY.md, "After a value reaches the VM"). That's expected: don't reset the VM to get them back unless the user asks.

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

**One approval for username and password:** `metacomputer secrets login NAME --username-selector '#user' --password-selector '#pass' [--profile P]` (selectors from a `dom_refs_v1` snapshot, each matching exactly one field) fills both into the page's fields with one question instead of two. Leave out `--username-selector` on a password-only page. The answer lists `fields`, the `origin` and a masked `username_hint`. Then submit with a click and check the page. `--origin https://site` allows only that one of the login's sites, and `--browser-pid PID` only the Chrome you opened (its `prepared_pid`); either way the fill refuses when two or more tabs there are on the site: close the others.

## Keeping the user signed in (sealed sessions)

1. Use a named profile (`isolated_named`, for example `x`).
2. Sign in: fill as above, or let the user sign in once through `metacomputer view`.
3. End the browser session (`end_session`): a profile Chrome is using can't be saved.
4. `metacomputer secrets session save NAME --profile x`. The user confirms it in their terminal.
5. Later, with no Chrome on that profile: `metacomputer secrets session load NAME`, then `browser_prepare` with `{mode: "isolated_named", name: "x"}`. A session loads only under the profile name it was saved from.

A session the user imported from their own Chrome (`list` shows it as cookies for its sites) loads into its profile's own cookie store: open that profile once first (`browser_prepare` with `{mode: "isolated_named", name: P}`, then `end_session`), then `session load NAME`, then `browser_prepare` with the same profile. If the reason says the profile has no cookie store yet, do that first step. One whose cookies reach an identity provider asks at every load. When you need only some of its sites, add `--site HOST` (repeatable, each exactly as `list` shows it): only the cookies those sites get go to the VM.

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
