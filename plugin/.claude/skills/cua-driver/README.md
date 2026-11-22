# Cua Driver agent skill

> **metacomputer's copy** of Cua Driver 0.53.0's skill pack, Linux guide only. Here Cua
> Driver runs only inside metacomputer's VM, which metacomputer's installer builds:
> see [the 5-minute install](https://github.com/metavert-io/metacomputer#the-5-minute-install).
> Don't install Cua Driver on your Mac or give it permissions there. The install and
> update sections below are adapted for this; the rest is Cua's text. Cua's license:
> [LICENSE](LICENSE).

This cross-agent skill teaches an AI agent to operate native applications on
macOS, Windows, and Linux with the
[`cua-driver`](https://github.com/trycua/cua/tree/main/libs/cua-driver/rust)
CLI or MCP server.

It covers the canonical snapshot-action-verify loop, exact window addressing,
accessibility and pixel actions, background/foreground delivery, typed browser
automation, session recording, and
platform-specific limitations. The skill
defaults to background delivery and requires structured refusal or observed
failure before a caller escalates to foreground input.

## Install Cua Driver

In metacomputer, Cua Driver runs only inside the VM. metacomputer's installer
downloads the pinned release (`CUA_DRIVER_VERSION` in `config/defaults.env`),
checks its SHA-256 and installs it in the VM when it builds the VM. Don't run
Cua's installer on your Mac, and don't grant Cua Driver Accessibility or Screen
Recording permission there. To set up metacomputer, see
[the 5-minute install](https://github.com/metavert-io/metacomputer#the-5-minute-install).

## Install the skill

This pack comes with metacomputer's Claude Code plugin, as `metacomputer:cua-driver`.
`clawhub install` and `cua-driver skills install` aren't needed.

## Reading order

- `SKILL.md`: lean entrypoint, mandatory safety rules, and task-based routing.
- `WORKFLOW.md`: exact targets, bounded snapshots, coordinates, action semantics,
  verification, filesystem and clipboard proof.
- `RUNTIME.md`: preflight, transport/session ownership, authorization, cursor,
  and cleanup.
- `LINUX.md`: host-specific launch, capture, accessibility, input delivery,
  permissions, and refusal boundaries (the macOS and Windows guides aren't in
  this Linux-only copy).
- `BROWSER.md`: exact browser-window binding, explicit profile preparation,
  page refs, trust-classified click/type/navigation, and native fallbacks.
- `VISUAL.md`: frozen (no new work). Load it only when the user explicitly asks for
  visual-region parsing or `jev-use`.
- `RECORDING.md`: trajectory evidence, MP4 capture, and replay.

The agent loads `SKILL.md`, then only the linked guide needed for its next
step. Every reference ships in the pack; no separate skill installation or
repository checkout is required. The installer keeps the flat Markdown layout.

## Browser model

Browser work starts from the same native `(pid, window_id)` selection as every
other app. `get_browser_state` binds that exact window to a session-scoped
target and tab, then returns short-lived page refs for `browser_click`,
`browser_type`, and `browser_navigate`.

Setup is never a hidden read side effect. `browser_prepare` requires explicit
approval before launching a driver-managed profile or attaching to an existing
authenticated profile. Trusted pointer input and synthetic DOM clicks are
reported as different routes; the driver refuses instead of silently changing
trust class or foregrounding a standalone browser.

See `BROWSER.md` for the supported surface and exact recovery rules.

## Recording

Session recording captures before/after state, screenshots, action metadata,
and optional MP4 video. macOS uses ScreenCaptureKit. Windows uses ffmpeg with
`gdigrab`. Linux uses compositor-specific capture or ffmpeg with `x11grab`.
Availability is reported honestly when a host dependency or portal grant is
missing. See `RECORDING.md`.

## Updates

The skill is versioned with Cua Driver releases, and metacomputer pins both: when
`CUA_DRIVER_VERSION` changes, `scripts/vendor-driver-skill.py` replaces this pack
with that release's, and a rebuilt VM gets the new driver. Don't run `cua-driver
skills install`, `skills update` or `update` from a task. The pack version does
not identify the running daemon; inspect the live tool schema when versions differ.

## License

Cua Driver and this skill pack are MIT licensed, copyright Cua AI, Inc.; the full
notice is in [LICENSE](LICENSE) beside this file. metacomputer's edits are under
metacomputer's own MIT license.
