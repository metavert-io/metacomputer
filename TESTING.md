# Testing

Everything here runs against your own VM, so start it first: `virtual-computer start`.
Results and recordings go to `results/`, `recordings/` and `screenshots/`
(all ignored by git).

## GUI suite: `virtual-computer test [A-F]`

Each test drives the VM only through Cua Driver's MCP tools (the same bridge
Claude Code uses), records an MP4 and per-action traces, and checks its
outcome independently: the accessibility tree, the driver's `verify_state`,
and files read back from the VM's disk.

| Test | What it does |
| --- | --- |
| A | Observes the desktop: apps, windows, trees, a dialog |
| B | GNOME Calculator: 137 × 29 by pressing its buttons; reads 3973 back |
| C | Firefox: example.com, a second tab, a Wikipedia search through the site's own form |
| D | LibreOffice Writer: types and styles a document, saves it, checks the file |
| E | Several apps together: copy text between apps, save, verify |
| F | Chrome through the driver's page tools: form (text, dropdown, check box, upload), download and rename, a confirm dialog, key-by-key typing |

`results/<run>/summary.md` lists each check.

## Chrome guard: `python3 tests/test_chrome_guard.py`

This test goes through `virtual-computer mcp-lazy`, the bridge Claude Code sessions use. It checks four things:
- background input aimed at Chrome is sent in the foreground;
- `set_value` and `run_script` aimed at Chrome are refused;
- a `run_steps` batch aimed at Chrome is sent in the foreground;
- the driver's leftover virtual input devices are removed before Chrome needs the keyboard.

Background is in
[docs/chrome-investigation.md](docs/chrome-investigation.md).

## Host sharing: `virtual-computer status`

With lume-secure and the default settings, the `host sharing` line must read
`clipboard off  audio off  rosetta off`. It's read from inside the VM: virtio
console port names, virtio device ids and virtiofs tags. Turning one setting
`on` (e.g. `SHARE_CLIPBOARD=on` in `config/local.env`) and restarting must
show just that one; all three `on` is Lume's own default. The flag parsing
and device selection have unit tests in
[lume-secure](https://github.com/metavert-io/lume-secure).

## Optional hardening: `tests/test_actionlog.py`, `tests/test_options.py`

`python3 tests/test_actionlog.py` needs no VM (16 checks). It writes logs to a
temporary folder, then tampers with them: an edited record, an edited record
with its hash recomputed, a deleted record, a cut-off end, and a log signed
with another key. It also runs the relay against a fake driver and checks that
messages pass through unchanged.

`VC_CONFIG=test-vm.env python3 tests/test_options.py [A] [B] [C]` runs against
a running **test** VM. It switches the VM's SSH and driver settings through the
maintenance account, then puts them back to the defaults.
- **A** (6 checks): a real session is logged in order, typed text is redacted,
  the file is private, and `actions verify` passes.
- **B** (12 checks): certificates work. These are refused: the static key, an
  expired certificate, one for another role, one from another CA, and one from
  another address. The VM forces the driver command for `mcp` even when the
  certificate doesn't, key IDs reach the VM's auth log, and the static keys
  work again afterwards.
- **C** (21 checks):
  - `readonly` refuses clicks, typing and launches.
  - The VM refuses a standard session while bounded.
  - `desktop` launches Mousepad by name and types into it. It refuses the
    terminal, page tools, and writes outside the task folders.
  - `browser` opens a listed origin and refuses another one and desktop input.
  - An expired manifest and one changed inside the VM are refused.
  - Standard mode works again afterwards.

The GUI suite also runs under the `desktop` profile: put
`DRIVER_MODE=bounded` in the test VM's config and run
`virtual-computer test A B C D`.
- E reads the copied passage with `clipboard_read`, which bounded mode refuses
  in driver 0.52.1.
- F uses page tools and hybrid typing, which need standard mode (or the
  `browser` profile for the page tools alone).

## Browser checks: `virtual-computer web …`

`compare`, `responsive`, `a11y` and `open-untrusted` serve as their own
smoke tests. The pages in `tests/fixtures/site/` cover the edge cases:
- a form with every control;
- a page with planted accessibility problems;
- a redirecting fake sign-in page.

To serve them in the VM:
`virtual-computer put tests/fixtures/site --to '~/virtual-computer-runs'`, then
`virtual-computer ssh ops 'cd ~/virtual-computer-runs/site && python3 -m http.server 8000 --bind 127.0.0.1'`.

## The skill, end to end: `tests/skill/run_headless.sh`

This runs a headless Claude Code session from any folder, with only the
`virtual-computer` tools and `virtual-computer` allowed, and summarizes what it did:
- the skill it used;
- the commands and tools it called;
- screenshots versus structural reads;
- the checks it ran;
- cost and the result.

```bash
tests/skill/run_headless.sh results/skill-tests OUT_NAME /some/other/folder "Get yourself a virtual computer and tell me what's on its screen."
```

Prompts worth covering:
- "use a computer to …";
- "in Chrome, drive the page directly";
- "check this page in both browsers";
- "what does this link do?";
- "take a screenshot of my screen", which must **not** use the VM;
- plain questions, which must not boot it.

Each session costs real API usage.

## Agentic check: `tests/agentic_claude.sh`

A headless session with only the MCP tools does a recorded calculator task by
reasoning over trees and screenshots, with no scripted steps.
