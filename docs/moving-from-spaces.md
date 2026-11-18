# Moving from Cua Spaces' Keyvault to `metacomputer secrets`

If you used Cua Spaces and now run Cua's open-source stack yourself (Lume, Cua Driver)
through metacomputer, you have probably used Spaces' **Keyvault**. It comes with every
Spaces plan, including the free one. Keyvault keeps your saved passwords and teleported
browser sessions, asks you before each use, and types a login into a Space's browser
without the agent seeing it. `metacomputer secrets` does the same job for metacomputer's
VM, and this page maps one onto the other.

metacomputer is MIT-licensed and shares no code with Spaces. Its vault is its own,
written from scratch. The compatibility described here covers names only: command verbs,
MCP tool names, parameters and audit event names, matched to Keyvault's public
documentation so that your habits and agents carry over. Nothing here talks to Spaces,
and nothing needs a Cua account.

## At a glance

| Keyvault (Cua Spaces) | metacomputer |
| --- | --- |
| `cua keyvault status` | `metacomputer secrets status` |
| `cua keyvault init` / `init --passphrase` | `metacomputer secrets init` (always a passphrase, typed twice; `--passphrase` is accepted). The vault is also bound to this Mac's Secure Enclave, and `init` prints a recovery key once |
| `cua keyvault unlock` / `unlock --passphrase` | `metacomputer secrets unlock [--for 30m]`, in your own terminal, which stays open and shows every request (`--passphrase` is accepted) |
| `cua keyvault lock` | `metacomputer secrets lock` (anyone may: it only restricts), or Ctrl-C in the unlock terminal |
| `cua keyvault import-passwords --browser chrome [--profile P] [--site S]` | `metacomputer secrets import-passwords --browser chrome [--profile P] [--site S]`; also `--csv FILE` for an export from Chrome, the macOS Passwords app, 1Password or Bitwarden, and `--dry-run` |
| `cua keyvault import-session --app chrome --site S` | `metacomputer secrets import-session --browser chrome --site S --name NAME --to-profile P` (`--app chrome` is accepted) |
| `cua keyvault requests [--json]` | `metacomputer secrets requests [--json]` |
| `cua keyvault approve ID` (Touch ID or your login password) | `metacomputer secrets approve ID` (Touch ID or your login password); `--passphrase` uses the vault passphrase instead |
| `cua keyvault deny ID` | `metacomputer secrets deny ID` |
| Unlocking an item (no prompt for it) | `metacomputer secrets policy NAME --approval session` |
| An unattended rule for a site and an agent, with an expiry | `metacomputer secrets rule add --site https://S --project DIR --for 8h` (or `--secret NAME`, `--session NAME`); `rule list`, `rule remove ID` |
| MCP `request_site_login` | the same tool name and parameters, served by metacomputer's hub (below) |
| MCP `teleport_browser_session` | the same tool name and parameters; it loads a session you imported (below) |
| MCP `teleport_app` | not applicable (below) |
| SDK `space.request_site_login(url, options)` | `Hub.request_site_login(url, options)` in `lib/mcomp_sdk.py` (synchronous), with `SiteLoginOptions` and `SiteLoginReport` |
| Audit `login.request`, `login.authorize`, `login.fill`, `login.denied` | the same names, in each record's `event` field beside metacomputer's own `op`; in webhook sinks as the CloudEvents extension `mcompevent`, in OTLP as `metacomputer.event` |
| Recovery key | `metacomputer secrets recover` with the recovery key `init` printed |

## The MCP tools

Both tools appear in the `metacomputer` MCP server's tool list beside Cua Driver's tools.
Each is served by the hub, so each goes through the hub's guards like any acting call:
- one controller at a time;
- bounded mode's manifest, which applies to the browser calls the tool makes;
- the action log, which names the client that asked;
- events.

The vault types the values through its own fill path, so the agent gets status, a site
and a masked username, never a password.

**`request_site_login`** takes `url` (required), `space`, `agent`, `session`, `target_id`,
`tab_id`, `username`, `request_id`, `wait_secs` and `relay_plaintext_ack`, as Keyvault's
does. It adds `profile` (a named driver profile to open) and `submit` (default true).
1. The first call finds the saved login for the URL's exact origin.
2. If you pass a tab (`session`, `target_id`, `tab_id` from `get_browser_state`), the tool
   uses it. If not, it opens page-mode Chrome on `url`.
3. It finds the password field and the username field before it.
4. It asks the vault to sign in. The answer is `status: "pending"` with a `request_id` while
   you decide.
5. Call again with that `request_id` (and `wait_secs`, default 20, at most 120). The
   answer is `status: "filled"` with `site`, `origin`, `username_hint` (`a***@example.com`),
   `submitted`, `page_url` (the page's origin only, never its path or query) and the tab, or
   `status: "denied"`. The vault fills only a tab on that one origin, and the form isn't
   submitted if the tab has left it.

**`teleport_browser_session`** takes `sites` (required), `browser`, `name`, `reason`,
`request_id` and `duration_minutes`. It adds `session` and `wait_secs`.
- It finds a session you imported that covers those sites, and loads only the cookies
  those sites get, even when the session holds more. The first call answers
  `consent_required` with a `request_id`; once you approve, the retry answers `moved: true`
  and names the driver profile to open with `browser_prepare`.
- If the profile has no cookie store yet, the hub has the driver create one first.
- If no imported session covers the sites, the error names the `import-session` command
  you run in your own terminal.

## What's the same

- Every use asks by default, and one approval covers one use (a sign-in's username and
  password together).
- Approving with Touch ID or your login password, from any terminal (`approve ID`). With
  `unlock --touch-id`, every question also shows that dialog by itself.
- Items you relax (`policy ... --approval session`) and unattended rules that expire.
  Making either needs you: the passphrase, or the dialog for a rule.
- **Identity providers always ask** and can't be relaxed. These are Google's, Microsoft's,
  Apple's and Okta's sign-in pages; the list is in SECURITY.md, "Identity providers always
  ask".
- A password is typed only on its login's exact origin; a look-alike or a redirect is
  refused before anything is typed.
- Cookies are copied into the sandbox's own browser, re-encrypted with that browser's key.
  Your Mac's Chrome key is never copied anywhere.
- A hash-chained audit log, with the site-login event names, the requester (Claude Code
  session and project), the origin, the VM and a masked username.
- Importing Chrome's saved passwords reads Chrome's "Chrome Safe Storage" key from your
  login keychain, after macOS asks you.

## What differs

- **The unlock terminal.** The vault is unlocked by `unlock` in a terminal you keep open.
  It shows each request and its outcome, and by default you approve there by typing the
  secret's name. `approve ID` is an extra way to answer, not a replacement. The vault
  locks when that terminal closes, on Ctrl-C, when its time is up, or with `lock`.
- **The passphrase is always typed.** There is no OS-key-store unlock without a passphrase,
  and no `--passphrase-stdin`: a passphrase piped in by a script could be piped in by any
  program running as you. With the Secure Enclave (the default), a copy of the vault file
  can't be opened on another Mac with the passphrase alone; the recovery key opens it.
- **Who asked is a Claude Code session and its project folder**, taken from the process
  tree, not a signed app identity. Unattended rules are keyed on that project folder
  (`--project DIR`) instead of an agent's name. Through these two tools, though, the
  requester is the hub itself, with no session or project, since it serves every session
  from one process: project limits and rules don't cover them, so they ask. The calling
  session and the `agent` parameter are shown to you, marked as unverified.
- **The use happens when you approve.** A `request_site_login` or
  `teleport_browser_session` that waits for you goes ahead as soon as you allow it. The
  retry with `request_id` collects the outcome rather than triggering the use. A request
  waits for you for up to 2 minutes; after that, it's refused.
- **One virtual computer per hub.** `space` (and `name` for the teleport tool) is
  accepted and ignored: the tools act on the VM their MCP server serves.
- **Saved logins can also be used directly.** Claude can run `metacomputer secrets fill
  NAME --field password|username|otp` or `login NAME --password-selector ...`, with the
  same approvals. TOTP codes and API keys (through a proxy with short-lived tokens) are
  metacomputer's own features.

## Not supported

- **`teleport_app`.** Spaces copies a Mac app's session into a macOS sandbox, but
  metacomputer's VM runs Ubuntu Linux, so a Mac app and its data can't run there. The
  equivalent for the web is `teleport_browser_session`. For a Linux app, install it in the
  VM (README, "Add apps") and sign in there.
- **Firefox sessions and Firefox passwords.** Sessions load into Chrome only. For Firefox
  logins, export a CSV from a password manager and use `--csv`.
- **localStorage and IndexedDB.** `import-session` brings cookies only. Chrome keeps
  localStorage in LevelDB stores that can't be merged into another profile reliably, so a
  site that keeps its sign-in there needs a fresh sign-in in the VM.
- **Saved passwords delivered into the sandbox's browser.** As in Keyvault, an agent can
  only use a password to sign in, never have it delivered. metacomputer has no option to
  write passwords into the VM's Chrome.
- **`duration_minutes`.** Loaded cookies stay in the driver profile until you delete it
  (`metacomputer browser profiles --delete NAME`) or reset the VM.
- **Relays and remote Spaces** (`relay_plaintext_ack`). Everything runs on your Mac.
- **The Keyvault page in the Spaces app** (items per app, site icons, "Disable Keyvault").
  `metacomputer secrets list`, `requests`, `rule list` and `audit` cover the same ground
  in a terminal.

## Bringing your logins over

Keyvault documents no export, so bring your logins in from where they came from. Run
these in your own terminal:

```bash
metacomputer secrets init                      # once: a passphrase, and a recovery key to write down
metacomputer secrets import-passwords --browser chrome --dry-run    # what would come in: sites and masked usernames
metacomputer secrets import-passwords --browser chrome              # macOS asks about Chrome's key: Allow
metacomputer secrets import-passwords --csv ~/Downloads/Passwords.csv   # the macOS Passwords app (File > Export), with 2FA codes
metacomputer secrets import-passwords --csv ~/Downloads/1password.csv   # or a 1Password, Bitwarden or Chrome export
metacomputer secrets import-session --browser chrome --site github.com --name github --to-profile github
metacomputer secrets bind                      # pin the VM (check its host key fingerprint, then type yes)
metacomputer secrets unlock --for 2h           # keep this terminal open while Claude works
```

- Everything imported asks before each use, as items do in Keyvault until you unlock them.
  Relax one with `policy NAME --approval session`, or allow one project for a while with
  `rule add`.
- A CSV export holds every password in plain text: delete it once it's imported.
- Then ask Claude to "log me into github.com in the virtual computer". It uses
  `request_site_login`, or `fill`/`login` directly, and you approve in the unlock terminal
  or with `metacomputer secrets approve ID`.

SECURITY.md, "Secrets and logins", has the full model: what each check holds and what it
doesn't.
