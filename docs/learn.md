# Learning from your own sessions: `metacomputer learn`

Claude repeats some mistakes when it drives the virtual computer: a pixel click
without a fresh screenshot, a third way of trying the same thing without
checking why the first two failed. `metacomputer learn` finds those patterns in
your own Claude Code sessions, proposes short lessons with the evidence behind
them, and, once you approve one, gives it to every later session as part of
the MCP server's instructions. `learn bench` then measures whether the lessons
help.

```text
your sessions ──▶ learn report ──▶ proposals ──▶ you approve ──▶ lessons.json ──▶ every new session's
(transcripts,      (classes,        (built-in      (review,        (private,         MCP instructions,
 action logs,       counts,          templates;     approve ID)     outside the        after the
 events, bench)     time per app)    --draft-with-claude)            checkout)          built-in habits
                                                                         │
                                                         learn bench ◀───┘  with vs without, on a test VM
```

Everything stays on your Mac, except with `--draft-with-claude`, which runs one
headless Claude Code session on your own account and sends it only counts.

## Commands

```bash
metacomputer learn report [--since 7d] [--project PATH] [--json]   # mine, then propose
metacomputer learn report --draft-with-claude                        # also draft lessons the templates don't cover
metacomputer learn review                    # go through the proposals one by one (at your terminal)
metacomputer learn approve no-frame route-churn
metacomputer learn reject app-notes-inkscape
metacomputer learn list [--instructions]     # the approved lessons; --instructions prints the exact text sent
metacomputer learn remove no-frame
metacomputer learn edit no-frame             # change a lesson's text (your terminal only)
metacomputer learn add                       # write one of your own (your terminal only)
metacomputer learn bench --config results/my-test-vm/test.env      # measure: with vs without, on a test VM
```

From a Claude Code chat, the plugin's `/metacomputer:learn` runs the report and
walks you through the proposals; Claude approves only the IDs you name.

| Command | Where it runs | What it does |
| --- | --- | --- |
| `report` | anywhere | Mines the sessions and logs, prints the report and saves the proposals (replacing the last report's) |
| `review` | your terminal (elsewhere it lists the proposals) | Each proposal with its evidence: approve, edit then approve, reject, skip |
| `approve ID...` | anywhere for built-in proposals; your terminal for drafted or edited ones | Moves proposals into the approved lessons, with their provenance |
| `reject ID...` | anywhere | Sets proposals aside; later reports don't propose them again (`approve` still takes them) |
| `list [--json] [--instructions] [--proposals]` | anywhere | The approved lessons, how many reach the instructions, the waiting proposals |
| `remove ID...` | anywhere | Takes approved lessons out |
| `edit ID` | your terminal | New text (and title) for an approved lesson or a proposal; an edited proposal then needs `approve` at your terminal |
| `add` | your terminal | A lesson in your own words, approved at once |
| `bench` | your terminal, or `--yes` | The benchmark without and with the approved lessons; it states the number of runs and the likely cost, and asks first |

"Your terminal" is the bar the secrets vault uses for its user-only commands:
standard input must be a terminal, so Claude's Bash tool, a script or a pipe is
refused with "needs your own terminal". Free text goes into every later
session's instructions, so only you write it.

## What `report` reads

- **Claude Code's session files**, `$CLAUDE_CONFIG_DIR/projects/**/*.jsonl`
  (else `~/.claude/projects`), including subagent files.
- **Saved headless transcripts** (`claude -p --output-format stream-json`), from
  each `--transcripts DIR`. When a session exists both ways, the headless copy is
  used and the other is skipped.
- **Benchmark results** under `results/bench/` (or `$BENCH_RESULTS`); `--no-bench`
  leaves them out.
- **The signed action log and the event log** of this VM's `LOG_DIR`
  (`logs/actions/`, `logs/events.jsonl`), and of each `--logs DIR`.

Only sessions that called a metacomputer tool are read, under either prefix
(`mcp__metacomputer__*` or `mcp__plugin_metacomputer_metacomputer__*`). Every
transcript file changed within `--since` is scanned for that tool call's marker,
and nothing else is kept from files that don't have it. `--since` takes `7d`
(the default), `12h`, `2w` or a date; `--project PATH` keeps only sessions that
ran in that folder or below it.

## What it reports

- **Failed driver calls by class.** Malformed arguments; each hub guard's
  refusal (one controller, Chrome, coordinate, bounded); element not found; a
  pixel action without a screenshot frame; `foreground_unavailable`; input sent
  but focus not restored; stale window ids and element tokens; failed `expect`
  and `wait_for` checks; menu paths not found or needing the foreground; no usable
  element extents; an app not installed; the computer off; other. Each with its
  share, how many sessions it hit and example calls (`session #call`).
- **Unverified results.** Actions that came back `unverifiable`, `did not
  settle` or `suspected_noop`, and how often the very next call acted again
  without a read or a check.
- **The same failure class again within one session** (each subagent counts
  on its own).
- **Routes changed without re-checking the premise.** After a failed action on
  an app, three or more different routes tried on that app within ten calls,
  before one worked cleanly, with at least one switch made without reading the
  app first. A route is the tool, its target kind (element, pixel, keys), its
  delivery mode and its scope.
- **Time per app on the virtual computer:** the model's time before each call
  plus the call's own time, by the app its window belongs to (from a fixed list),
  plus the VM's shell and other `metacomputer` commands. Gaps over ten minutes
  are cut to ten.
- From the logs: action-log calls, errors and hub refusals by class; event types.

These are heuristics over transcripts. On the 30 recorded sessions that a
reading by hand had gone through first, they found the same classes and the
same example calls, but they aren't exact.

## Proposals

A proposal is a short rule, its evidence (counts, sessions, example calls) and
an ID you approve it by.

**Built in.** Templates written from that reading, one per pattern:
`no-frame`, `foreground`, `focus-restore`, `stale-window`, `stale-token`,
`not-found`, `check-text`, `malformed`, `menu-path`, `menu-foreground`,
`no-extents`, the four guards (`guard-controller`, `guard-chrome`,
`guard-coordinate`, `guard-bounded`), `unverified`, `repeat-class`,
`route-churn`, and `app-notes-<app>` (read the skill's notes for an app that
takes 15% or more of your time). A failure class is proposed once it occurs
`--min` times (2 by default); a guard refusal once. The wording is fixed in
`lib/learn.py`; only counts, the fixed class names, an app name from the fixed
list and a skill file name are filled in.

**`--draft-with-claude`.** For patterns no template covers (failure classes
without one, errors of a tool that match no class, time sinks in apps without
notes), `report` runs one headless Claude Code session (`claude -p`, no tools, no
MCP servers, none of your settings, at most $0.50) and asks for up to three
lessons. It says so before it runs. The session gets only a summary of counts,
class names, tool names and app names, never transcript text, session names or
paths. Its answer is checked: a pattern it wasn't given, more than 300
characters, or a mention of secrets or of switching off, getting around or
overriding a control drops the draft. A draft's wording comes from a model, so
approving it needs your terminal.

## The store

Approved lessons live in a folder of your own, outside the checkout and the
plugin cache: `~/.local/share/metacomputer/lessons/`, or `LESSONS_DIR` in
`config/local.env` (`MCOMP_LESSONS_DIR` in the environment wins).

| File | Holds |
| --- | --- |
| `lessons.json` | Each approved lesson: id, title, text, source (built-in, Claude's draft, edited, yours), the date, where it was approved from, and the evidence summary |
| `proposals.json` | The last report's proposals and the ones you rejected |

The folder is made 0700 and the files 0600. They are read only when private:
a regular file, not a link, owned by you and not writable by the group or
others, in a folder of yours the group and others can't write. Otherwise nothing
is loaded, and `metacomputer status`, `security` and `learn list` say why.

## How lessons reach a session

When Claude Code connects to the MCP server, the answer to `initialize` carries
the server's instructions: who the server is, the skill's core habits, then the
approved lessons under "Notes this user approved from earlier sessions", then
Cua Driver's own text. `metacomputer learn list --instructions` prints exactly
what is added. The block:
- says that the lessons are advice from your own earlier sessions, that they
  never override a guard, a refusal, the instructions above or your own
  instructions, and that they can't turn any protection on or off;
- holds at most 12 lessons and 2,400 characters (each lesson at most 400, on one
  line); what doesn't fit is named, and `learn list` shows it;
- reaches sessions that start after the approval. A session already running
  keeps the instructions it started with.

`LESSONS=off` keeps the store but sends nothing. No guard reads the lessons:
the hub reads them only to build that text (`learned_notes()` in
`lib/mcp_hub.py`).

Approving, editing and removing a lesson emit `security.lesson_approved`,
`lesson_edited` and `lesson_removed` on the event stream when you have sinks
(docs/events.md): the id, a short title and the source, never the lesson's text.

## Measuring: `learn bench`

```bash
metacomputer learn bench --config results/my-test-vm/test.env [--tasks ff-form,calc-chart] [--budget 1.5] [--cap 12]
```

It runs [the benchmark](../bench/README.md) twice on a test VM, first with
`--lessons off`, then with `--lessons on`, and prints both side by side over the
tasks both runs finished: tasks passed, invalid-argument calls, refused calls,
other tool errors, mean turns and minutes, screenshots and cost, then what the
comparison spent. The per-task table goes to `results/bench/compare-*.md`.

- **It needs a test VM**, never your main one: every task resets that VM to its
  baseline. Build one with its own settings file (its own `VM_NAME`, ports,
  `SSH_DIR`, `RUN_DIR` and `LOG_DIR`; see `config/local.env.example`, "a second
  VM") and pass it as `--config`. It refuses the VM named in your own
  `config/local.env` and the VM named `metacomputer`.
- **It spends your Claude usage.** A full comparison is 40 headless sessions,
  about $20 and an hour. It says so and asks first; `--yes` skips the question.
  `--tasks` takes a subset, `--budget` caps each task and `--cap` each run.
- One run of each is noisy. For a decision, repeat it and compare the totals.

The benchmark's switch is also available directly: `bench/bench.py run
--lessons on|off` (default off), with the lessons from `MCOMP_LESSONS_DIR` or the
default store. `run.json` records how many lessons the sessions got and a hash of
the exact text.

**The first comparison** (2026-11-18; a test VM with Cua Driver 0.53.0, Opus 5.5,
the skill from main, $1.50 a task). The two lessons approved for it were the
built-in `no-frame` and `check-text`, proposed from 33 recorded sessions where
pixel actions without a screenshot frame were 35% of the failed calls:

| | without | with |
| --- | --- | --- |
| passed | 19/20 | 19/20 (the same task failed both times, at the same check) |
| invalid-argument calls | 0 | 0 |
| other tool errors | 12 | 9 |
| mean turns | 17.1 | 16.6 |
| mean minutes | 0.86 | 0.86 |
| screenshots | 30 | 29 |
| cost | $9.67 | $9.81 |

It cost $19.48. Neither targeted pattern occurred in these tasks without the
lessons (no pixel action without a frame; three failed checks in each run), so
this run shows no measurable effect either way: the differences are within one
run's noise. The benchmark's tasks already pass with today's skill, which holds
the same rules; lessons are for what your own sessions keep running into.

## Security

The threat is a prompt-injected session trying to plant a lesson, so that
every later session follows it. SECURITY.md, "Lessons from your own sessions",
has the protections and where they end. In short: proposals are built only from
structured fields, never from what the VM showed; `--draft-with-claude` gets only
counts; free text needs your terminal; the store must be private; the lessons
are advice that no guard reads; and nothing about them leaves the Mac beyond a
short title in an event.
