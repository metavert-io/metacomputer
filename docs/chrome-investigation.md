# Chrome in the virtual computer: investigation (2026-11-16)

Outcome: Chrome is an install-time option next to Firefox (the default). How the skill uses it: `skills/metacomputer/BROWSER.md`.

Question: should the virtual computer offer Google Chrome next to Firefox, and can Claude
drive it either like a person (accessibility tree, keys, clicks) or directly
through the page (DOM)? Firefox stays the default either way.

Setup: a separate test VM, built with a settings file of its own
(`MCOMP_CONFIG=test.env metacomputer build`, with a different `VM_NAME` and
`BROWSERS="firefox chrome"`). The working VM and its
baselines were not touched. Test pages were served inside the VM from
`127.0.0.1:8000` (a form with a text field, dropdown, check box, file input,
confirm() button, download link and a second page).

## Install

- Google now ships official Chrome for Linux arm64: `google-chrome-stable`
  157.0.8103.78 from `https://dl.google.com/linux/chrome/deb/` (Release lists
  `amd64 arm64`). The signing key's fingerprint
  `EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796` matches the one Google publishes
  at https://www.google.com/linuxrepositories/; it's pinned in `config/defaults.env`.
- Provisioning (`chrome` in `BROWSERS`): the key goes to
  `/etc/apt/keyrings`, the repo entry is our own
  `metacomputer-google-chrome.sources`, and `/etc/default/google-chrome` stops the
  package's cron job from adding its own entry. The entry can't be named
  `google-chrome.sources`: the package's postinst rewrites that file whenever it
  exists, pointing it at a keyring the package's scripts write themselves
  (found 2026-11-18; VMs built before then have the rewritten entry). The build
  runs the daily job once and fails if any other entry for Google's repo exists.
- Policies in `/etc/opt/chrome/policies/managed/metacomputer.json` turn off
  sign-in, sync, metrics, password saving, autofill, Privacy Sandbox prompts,
  generative-AI features and default-browser prompts.
- Firefox stays the default: XFCE's preferred browser, the https handler and
  `x-www-browser` / `gnome-www-browser` all still point at Firefox
  (`metacomputer inspect system`).
- The executable is root-owned under `/opt/google/chrome/`, which is what
  the driver requires before it will launch Chrome itself.
- A normal first launch shows Google's "Additional Terms of Service" dialog
  (readable through accessibility). We did not accept it; `--no-first-run`
  (also what the driver uses) skips it.
- A normal launch contacts Google in the background (time sync, component
  updates); the driver's own launches disable background networking.

## Human-like mode (accessibility tree + keys/clicks, as Firefox today)

| What | Chrome | Firefox (for comparison) |
| --- | --- | --- |
| Page content in the tree | Yes, rich: form, labelled entries, combo box, check box, buttons, links (the driver's `launch_app` adds `--force-renderer-accessibility`) | Yes, fewer labels |
| Clicks by accessibility action (buttons, links, check boxes) | Background, work | Background, work |
| `run_steps` by role/name + `expect` | Works | Works |
| `set_value` on a page field | Worked once, but uses the driver's virtual keyboard (see below) | Ignored |
| Typing | Foreground only | Foreground only |
| Dropdown | Foreground: Tab into it and type the option ("Blue") | Not tested |
| File upload | Chrome's file picker isn't in the accessibility tree; typing the path in the foreground closed it without attaching. Not working | Not tested |
| `verify_state` on page elements | Not tested | Sees no page elements |

Verified run (clean session): a foreground click on Name, then typing,
then Tab + "Blue", then accessibility clicks on "I agree" and Submit gave
`name=Human Mode colour=blue agree=true` in 8 s.

**Blocking issue: the driver's virtual input devices break Chrome's
keyboard.** One background action that goes through the driver's virtual
pointer or keyboard (a background pixel click, background `type_text`, or
`set_value` on a page field) leaves Chrome ignoring all keyboard input for
the rest of the X session:
- foreground keys from the driver are ignored, and so are `xdotool` keys;
- relaunching Chrome doesn't help, nor does ending the driver session;
- restarting the desktop session (`metacomputer-admin restart-desktop`)
  fixes it.

Reproduced three times:
- in a fresh session, keys worked;
- one background pixel click was enough to break them;
- clicks by accessibility action never broke them.

Firefox is not affected.

**Root cause.** The driver's background input route (path `mpx_uinput` /
`mpx_pointer`) creates an XInput2 master pointer/keyboard pair named
`CUA v1.…`, and it can stay after the action and after the driver session
ends (seen with `xinput list`). While any such pair exists, Chrome ignores
all keyboard input, and that holds for **any** app's background input: a
background `type_text` into the Calculator was enough.

**Fix.** `xinput remove-master` on the pair restores Chrome's keyboard at
once, with no restart. The project ships `input-reset` (guest) /
`metacomputer input-reset`. The lazy bridge (since the hub, `lib/mcp_hub.py`, for every client):
- sends input aimed at Chrome in the foreground;
- refuses `set_value` and `run_script` aimed at Chrome;
- runs the reset before Chrome's next keyboard input whenever a
  virtual-device route was used.

One caveat: a driver session whose pair was removed gets an error on its
next background-keyboard action ("could not take focus") until it
reconnects. `tests/test_chrome_guard.py` passes 8/8.

Rule for human-like Chrome: accessibility actions plus foreground input
only, never background pointer or keyboard routes.

## Page-level (DOM) mode: the driver's Chrome tools

`browser_prepare {allow_launch: true, profile: {mode: "isolated_new"}}`
launched a driver-owned Chrome on a throwaway profile in 0.14 s. Nothing
else changed (`side_effects` all false except `created_profile` and
`launched_browser`). `get_browser_state` then bound it exactly
(`binding_quality: exact`, `mutation_allowed: true`).

| What | Result |
| --- | --- |
| `browser_navigate` | Instant |
| `semantic_v2` snapshot | Typed refs with actions (`type`, `click`, `upload`), the dropdown with every option and which is selected, check box state, plus an outline of the page |
| `dom_refs_v1` snapshot | Real elements: tag, id, name, `href=report.txt`, `type=file` |
| `browser_type` | Background, 0.5 s, works |
| `browser_select` | Background, works ("Blue") |
| `browser_set_input_files` | Background, works (`file=upload.txt`) |
| `browser_click` (trusted) | Refused in the background on Linux; works with `delivery_mode: "foreground"` |
| `browser_click` `input_route: "dom_event"` | Background, works (check box, Submit, link to the next page) |
| `browser_download` | Saved to the approved folder; content right, under the download's id (the result has only `download_id` and `bytes`). `metacomputer rename` gives it the name from the link's `href` (`dom_refs_v1`) |
| `browser_dialog` | Works: the click result names the dialog it opened; `inspect`, then `accept`/`dismiss` with `delivery_mode: "foreground"` (confirm → "yes"; an alert from a synthetic click too). An earlier failure was in the first test page, not the driver |
| Viewport screenshot (`include_screenshot`) | 8 KB PNG through Chrome, window not brought forward |
| Legacy `page` tool | Falls back to the accessibility tree on Linux, as in Firefox (no `href`s) |
| Recording | The MP4 shows each field filling in, with the agent-cursor marker |
| Form fill + submit + read | About 1.5 s of tool time (7.1 s including launch and recording) |

Profiles and lifecycle:
- `isolated_new` is thrown away when the session ends.
- `isolated_named` persists: a local-storage counter read `visits=1`, the
  session ended (Chrome closed), and a new session relaunched it and read
  `visits=2`.
- A driver-launched Chrome closes after 300 s without driver activity, and
  when its session ends.
- A driver-launched Chrome doesn't expose its page to the accessibility
  tree (its launch omits `--force-renderer-accessibility`, and
  `ACCESSIBILITY_ENABLED=1` doesn't help). So hybrid mode reads through
  page snapshots and acts with trusted clicks (`delivery_mode: "foreground"`)
  and key-by-key typing (`browser_type mode: "keystrokes"`, verified).

Attaching to an ordinary running Chrome (`strategy: existing_profile`) is
refused: "existing-profile attachment in standard mode requires --grant
existing-profile". That would be a launcher flag on the guest's driver;
not enabled.

## Evidence

- Recording of page-level mode: a task folder under `results/` (MP4 and
  contact sheet).
- Provisioning log: `logs/provision-*.log` from the test build.

## Reported upstream

- [trycua/cua#7930](https://github.com/trycua/cua/issues/7930): after the
  background (MPX) input route runs, Chrome ignores all keyboard input until
  the `CUA v1.…` master pair is removed.
- [trycua/cua#7931](https://github.com/trycua/cua/issues/7931): `invoke_menu`
  that opens a modal GTK 3 dialog (Mousepad's Save As) leaves the app
  unresponsive to AT-SPI.

Both are still open, and both still happen with Cua Driver 0.53.0 (checked
2026-11-18): background typing leaves a `CUA v1.…` pair behind, and after
`invoke_menu` File > Save As Mousepad's windows read back as one-element
partial trees.
