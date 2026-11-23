---
name: learn
description: "Learn from your own metacomputer sessions: mine them for repeated mistakes on the virtual computer, then go through the proposed lessons with the user. Use when the user asks to learn from past metacomputer sessions, review metacomputer lessons, or runs /metacomputer:learn (optionally with --since 7d or --project PATH)."
allowed-tools: Bash(~/.local/bin/metacomputer learn report:*), Bash(~/.local/bin/metacomputer learn list:*), Bash(~/.local/bin/metacomputer learn review:*)
---

Help the user turn their own sessions on the virtual computer into lessons for later sessions
(`metacomputer learn`; the project's docs/learn.md has the details). It reads the sessions of every coding agent
metacomputer knows on this Mac (Claude Code, Codex, Gemini CLI, Antigravity CLI, OpenClaw, Hermes Agent) that used the
metacomputer tools.

1. Run `~/.local/bin/metacomputer learn report` as one shell command, with nothing else on the line; add
   `--since 7d` (or `12h`, `2w`, a date) or `--project PATH` only when the user gave them. It reads only this Mac's
   sessions that used the metacomputer tools and changes nothing but the list of proposals. If it found no sessions,
   say so and stop. (No `~/.local/bin/metacomputer`? metacomputer isn't set up on this Mac: say so, and stop.)
2. Sum up the report in a few lines: the most common failure classes with their counts, how many
   sessions repeated a failure, the runs of changed routes, and where the time went.
3. Go through the proposals as a numbered list: each one's ID, its rule and its evidence. Say which
   ones need the user's own terminal (drafted or edited text).
4. Ask which to approve and which to set aside. Then run
   `~/.local/bin/metacomputer learn approve ID ...` and `~/.local/bin/metacomputer learn reject ID ...`
   with exactly the IDs the user named. Never approve one they didn't name, and never all of them by default.
5. For a drafted or edited proposal, a change to a lesson's text, or a lesson in their own words, give
   the user the command to run in their own terminal (`metacomputer learn approve ID`, `learn edit ID`,
   `learn add`). Those refuse to run without a terminal on purpose; never try to get around that.
6. Finish with `~/.local/bin/metacomputer learn list`: approved lessons reach sessions that start from
   now on, as advice that never overrides a refusal or the user.

Only if the user asks: `learn report --draft-with-claude` drafts lessons the built-in ones don't cover
(one headless Claude Code session on their account), and `learn bench --config TEST_ENV` measures the
lessons on a test VM (40 sessions, about $20; it asks first). Don't run either on your own.
