# Browsers on the virtual computer

Verified on Cua Driver 0.52.1 with Firefox (Mozilla's arm64 build) and Google
Chrome 157 (Google's arm64 build) on 2026-11-16. Background:
`docs/chrome-investigation.md` in the project folder (`metacomputer where`).

## Choose a browser and a mode

Defaults come from `~/.local/bin/metacomputer browser` (out of the box:
Firefox, human). Words in the request override them.

| Mode | What Claude does | Browsers | Good for |
| --- | --- | --- | --- |
| **human** (default) | Reads the accessibility tree; clicks and types in the window like a person | Firefox, Chrome | Demos, recordings, visual and UX checks, "can a real user get through this" |
| **page** | Works on the page's own elements through Chrome's tools, in the background | Chrome | Forms, data entry, dropdowns, uploads, downloads, dialogs, reading a site exactly; fastest |
| **hybrid** | Reads and checks through the page; acts with real mouse clicks and key-by-key typing that show in a recording | Chrome | Realistic recordings with exact verification |

- **Request words:**
  - "in Chrome", "use Chrome" → Chrome.
  - "drive the page directly", "use the DOM", "fast", "fill this form" (with Chrome as the default) → page.
  - "like a person", "human-like", "realistically" → human.
  - "realistic but verified", "show it typing" → hybrid.
- **Page or hybrid in Firefox:** the driver has no Firefox page route. Say so in one line and offer Chrome (or human mode in Firefox).
- **Chrome not installed** (`metacomputer inspect browser`): say so; adding it is a VM rebuild, which needs the user's OK.
- **Announce the choice once, before acting**, for example: "Using the virtual computer: Chrome, page-level, throwaway profile."

## Human mode

**Firefox:** see [DESKTOP.md](DESKTOP.md#firefox).

**Chrome:**
- **Launch.** `launch_app {launch_path: "/usr/bin/google-chrome-stable", additional_arguments: ["--no-first-run", "--no-default-browser-check", URL]}`.
  - The driver turns on page accessibility for apps it launches.
  - `--no-first-run` skips Google's terms-of-service dialog. Never accept terms on the user's behalf.
- **Read.** `get_window_state {query, include_screenshot: false}`. Chrome labels form controls well: `entry "Name"`, `combo box "Colour"`, `check box "I agree"`.
- **Click.** By element token, or `run_steps` by role and name, as in other apps.
- **Type in the foreground.** Click the field, then `type_text` *without* an element token, both with `delivery_mode: "foreground"`. Verified example:
  ```json
  run_steps {"pid": P, "window_id": W, "delivery_mode": "foreground", "steps": [
    {"click": {"role": "entry", "name": "Name"}},
    {"tool": "type_text", "args": {"text": "Ada"}},
    {"tool": "press_key", "args": {"key": "Tab"}},
    {"tool": "type_text", "args": {"text": "Blue"}}]}
  ```
  Tab into a dropdown and type the option's text to choose it.
- **Never use background input or `set_value` on a Chrome page.**
  - Background input goes through the driver's virtual devices. Chrome ignores it, and while those devices exist Chrome ignores the keyboard altogether.
  - The bridge enforces this: it sends input aimed at Chrome in the foreground, and refuses `set_value` and `run_script` aimed at Chrome, naming the route to use.
  - Before Chrome gets keyboard input, the bridge removes devices left by earlier background actions (its note says so).
  - Manual fix: `metacomputer input-reset`.
- **File uploads:** Chrome's file picker isn't in the accessibility tree. Use page mode.
- **Profile:** the VM's normal Chrome profile (`~/.config/google-chrome`). It persists until a reset.

## Page mode (Chrome)

Use the same `session` label on every call. Refs change after each snapshot and each navigation.

1. **Launch.** `browser_prepare {session: S, allow_launch: true, profile: {mode: "isolated_new"}}` launches Chrome on a throwaway profile and returns `prepared_pid`. Use `{mode: "isolated_named", name: N}` only when the user wants a profile kept (see Profiles).
2. **Window.** `list_windows {pid}`, then maximize it: `set_window_frame {x:0, y:27, width:1920, height:1000}`.
3. **Bind.** `get_browser_state {pid, window_id, session}` returns `target_id` and `tab_id`. Go on only when it says `binding_quality: "exact"` and `mutation_allowed: true`.
4. **Navigate.** `browser_navigate {target_id, tab_id, session, url}`. Only `http`, `https` and `about:` work.
5. **Read.**
   - `get_browser_state {target_id, tab_id, session, snapshot_format: "semantic_v2"}` returns typed `refs`: a textbox with `type`, a combobox with its options, a check box with its state, a file button with `upload`, links. It also gives an `outline` of the page. Add `query` to narrow it.
   - `snapshot_format: "dom_refs_v1"` gives the real elements: tag, id, name, `type`, `href`.
6. **Act** (all verified in the background unless noted):
   - `browser_type {ref, text}`, with `replace: true` to overwrite;
   - `browser_select {ref, label}`;
   - `browser_set_input_files {ref, files: [guest paths]}`; copy the files in first with `metacomputer put`;
   - `browser_click {ref, input_route: "dom_event"}`, or `{ref, delivery_mode: "foreground"}` for a trusted click (the plain background trusted click is refused on Linux);
   - dialogs: the click result says when a page opened one. Then `browser_dialog {action: "inspect"}`, followed by `{action: "accept" | "dismiss", dialog_id, delivery_mode: "foreground"}`.
7. **Verify.** Take a fresh `semantic_v2` snapshot with a `query` for the expected text.
8. **Downloads.**
   - `browser_download {session, target_id, tab_id, ref, destination_root: FILES_DIR}` (the folder must exist; `metacomputer task start` makes it).
   - The file is saved under its `download_id`. Rename it with `metacomputer rename FILES_DIR/<download_id> <name>`, taking the name from the link's `href` in `dom_refs_v1`.
9. **Page screenshot.** `get_browser_state {..., include_screenshot: true}` returns the viewport without bringing the window forward.
10. **Finish.** `end_session {session}` closes the browser and deletes a throwaway profile.

Things to know:
- **It times out.** A driver-launched Chrome closes after 300 s without driver activity, and when the session ends.
- **No accessibility tree.** A driver-launched Chrome doesn't expose its page to the accessibility tree, so read it through snapshots, not `get_window_state`.
- **Speed.** Filling and submitting a four-field form took about 1.5 s of tool time.

## Hybrid mode (Chrome)

- **Setup:** the same as page mode. Read and verify with snapshots.
- **Clicking:** `browser_click {ref, delivery_mode: "foreground"}`, a trusted click at the element.
- **Typing:** `browser_type {ref, text, mode: "keystrokes"}`, which types key by key.
- **Recording:** start it first, so the video shows the clicks and typing.

## Profiles and sign-ins

- **Throwaway by default.**
- **Named profiles,** when the user asks for one (for example "use a profile called shopping"):
  1. Launch with `{mode: "isolated_named", name: "shopping"}`.
  2. Sign in: fill it from the vault (next section), or ask the user to sign in themselves through `metacomputer view`, in the window Claude opened.
  3. Later sessions with the same name reuse the cookies.
- **Never type the user's credentials yourself.** Use `metacomputer secrets` (next section).
- **Housekeeping:** `metacomputer browser profiles` lists named profiles, and `metacomputer browser profiles --delete NAME` deletes one (ask first). A VM reset removes them all.
- **Never attach to Chrome's normal profile.** `strategy: existing_profile` isn't enabled in this lab.

## Signing in: `metacomputer secrets`

The user can keep passwords, one-time-code seeds and saved browser logins in a vault on the Mac. Claude uses them without ever seeing them. Background: SECURITY.md, "Secrets and logins", in the project folder.

- **Never ask for a password, a code or the vault passphrase in chat.** Never type one with `type_text`, `browser_type` or a shell command.
  - If the user offers one, ask them to keep it out of the chat and store it themselves, in their own terminal: `metacomputer secrets set NAME --kind password --origin https://site --username U`.
- **See what's stored:** `metacomputer secrets list`. It shows names, sites, usernames and which `--field`s each secret fills, never values. It works only while the vault is unlocked.
- **Locked (exit 3):** ask the user to run `metacomputer secrets unlock` in their own terminal, then try again. If there's no vault yet, the message says so: ask them to run `metacomputer secrets init` and `set` themselves.
- **Only page-mode Chrome.** Fills work in Chrome launched with `browser_prepare`, not in Firefox or human-mode Chrome.
- **To fill a login:**
  1. Open the sign-in page in page mode (`browser_prepare`, `browser_navigate`).
  2. Focus the field with a trusted click: `browser_click {ref, delivery_mode: "foreground"}`. A `dom_event` click may not focus it.
     - Or let the fill focus it: add `--selector '#username'` (an id or name from a `dom_refs_v1` snapshot). It must match exactly one element.
  3. `metacomputer secrets fill NAME --field username`.
  4. Focus the password field, then `metacomputer secrets fill NAME --field password`.
  5. For a one-time code, focus the code field, then `metacomputer secrets fill CODE_NAME --field otp`.
  6. With a named profile, add `--profile P` to each fill.
  7. Submit with a click, then check the signed-in page with a fresh snapshot.
- **Read the result.**
  - `{"ok":true,...,"chars":N}` means it was typed and checked.
  - Exit 2: refused, with the reason (wrong site, wrong field, or the user said no). Fix the cause, for example focus the right field, and try once more. Never work around a refusal.
  - Exit 4: an error, such as the VM being off, no Chrome, or no tab on that site.
- **Don't read a filled field back.** A page script, or a snapshot that includes a field's value, would bring the secret into the conversation. The fill's own `chars` check is the verification.
- **The user may need to answer.** A secret set to `each`, or a vault unlocked with `--confirm each`, waits up to 2 minutes for the user's y/N in their terminal. Tell them a confirmation is waiting.
- **Recordings:** password fields show dots, but usernames and codes are visible on screen. Don't record those steps unless the user wants that.
- **To keep a login** across sessions or VMs:
  1. Use a named profile (`isolated_named`, for example `x`).
  2. Sign in: fill it as above, or let the user sign in once through `metacomputer view`.
  3. End the browser session (`end_session`): a profile Chrome is using can't be saved.
  4. `metacomputer secrets session save NAME --profile x`.
  5. Later, with no Chrome running on that profile: `metacomputer secrets session load NAME --profile x`, then `browser_prepare` with `{mode: "isolated_named", name: "x"}`.

## Checks that need no model: `metacomputer web`

Each runs in its own throwaway browsers and writes `report.md`, `report.json` and images to a results folder.

| Command | Use when the user asks… |
| --- | --- |
| `metacomputer web compare URL` | "does it look the same in Firefox and Chrome": side by side, plus differences in headings, links, controls and text |
| `metacomputer web responsive URL [--browser chrome\|firefox] [--widths 1920,1280,768,500] [--baseline DIR]` | layouts at several widths, or "what changed since last time" (similarity scores and difference images against an earlier run's folder) |
| `metacomputer web a11y URL [--browser chrome\|firefox]` | an accessibility smoke check: unlabelled controls, images without text alternatives, missing headings, vague links. Chrome finds more |
| `metacomputer web open-untrusted URL [--watch 10]` | "what does this link do / is it safe to open": server and in-page redirects, the final page, password prompts, downloads started (which stay in the VM). Nothing on the page is clicked |

Options: `--task NAME` puts the results in that task's folder, `--record` records the screen, `--json` prints the report as JSON. `--override` runs while another session is using the computer (ask first).

`open-untrusted` also sends what it found (password prompts, downloads, a redirect to another site) to the user's event sinks, if they set any up. Don't `notify` about it again.

## When a page needs the user

- **What counts:** a sign-in, a CAPTCHA, a one-time code, or an approval only they can give.
- **Run `metacomputer notify`** with the site's name (not the link) and what to do, for example: `metacomputer notify "Sign-in needed at example.com to export the invoices" --why login --action "Sign in through the live view (metacomputer view), then tell Claude to carry on"`.
- **Say it in your reply, then wait.** [EVENTS.md](EVENTS.md) has the `--why` values and what's sent automatically.
- **Never solve a CAPTCHA through another service,** and never look for a way around a sign-in.
