# Testing

Everything here runs against your own VM, so start it first: `claude-vc start`.
Results and recordings go to `results/`, `recordings/` and `screenshots/`
(all ignored by git).

## GUI suite: `claude-vc test [A-F]`

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

This test goes through `claude-vc mcp-lazy`, the bridge Claude Code sessions use. It checks four things:
- background input aimed at Chrome is sent in the foreground;
- `set_value` and `run_script` aimed at Chrome are refused;
- a `run_steps` batch aimed at Chrome is sent in the foreground;
- the driver's leftover virtual input devices are removed before Chrome needs the keyboard.

Background is in
[docs/chrome-investigation.md](docs/chrome-investigation.md).

## Browser checks: `claude-vc web …`

`compare`, `responsive`, `a11y` and `open-untrusted` serve as their own
smoke tests. The pages in `tests/fixtures/site/` cover the edge cases:
- a form with every control;
- a page with planted accessibility problems;
- a redirecting fake sign-in page.

To serve them in the VM:
`claude-vc put tests/fixtures/site --to '~/cua-lab-runs'`, then
`claude-vc ssh ops 'cd ~/cua-lab-runs/site && python3 -m http.server 8000 --bind 127.0.0.1'`.

## The skill, end to end: `tests/skill/run_headless.sh`

This runs a headless Claude Code session from any folder, with only the
`cua-linux` tools and `claude-vc` allowed, and summarizes what it did:
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
