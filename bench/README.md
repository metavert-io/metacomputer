# metacomputer benchmark

A fixed set of 20 desktop tasks. Each one runs in a fresh headless Claude Code
session on a test VM, and a check reads the VM's real state afterwards to score
it. The point is to compare like with like: keep the model, the harness, the
tasks and the budgets the same, change one thing, and see what moves. That
thing could be the skill, the shell channel, a bridge feature, the screen size
or the driver.

The method follows Microsoft's Hybrid Computer Use study
([HCU](https://github.com/microsoft/Hybrid-Computer-Use)), which compared
desktop interfaces this way.

## What it measures

For each task:
- **pass** (every check met) and a **partial score** from 0 to 1 (the checks
  are weighted);
- **cost** in USD, **turns**, **minutes**, and input and output **tokens**;
- **the tool-call mix:**
  - screenshots: images returned by any tool;
  - tree reads: `get_window_state`, `verify_state`, `get_browser_state`;
  - actions, by route:
    - accessibility (an element token, a role and name, a menu path, `set_value`);
    - pixel (x, y);
    - keyboard;
    - app (`launch_app` and the like);
    - page (Chrome's `browser_*` tools);
  - `run_steps` batches;
  - shell use (`metacomputer ssh` or `metacomputer sh` from Bash);
  - other `metacomputer` commands;
- **refused and rejected calls:**
  - calls the bridge or driver refused;
  - Bash calls the permissions denied;
  - calls rejected for invalid arguments;
  - other tool errors, with the first few error texts kept for a look.

Checks never trust what the agent says. They read the VM's real state:
- the saved files (Mousepad text, `.odt`, `.docx`, `.ods` and `.csv`, read
  with Python's `zipfile` and XML parsers, not LibreOffice);
- what the browser really sent to the fixture web server, which logs every
  request, its form fields, referrer and user agent;
- app settings (`gsettings`);
- the calculator's display, copied with `xdotool` and `xclip` after the session.

## Before you run it

- **Use a test VM, never your main one.** Each task resets the VM to its
  baseline first. The runner refuses to start without `MCOMP_CONFIG`, and it
  refuses the VM named `metacomputer` unless you pass `--allow-main-vm`.
- **Make sure nobody else is using that VM** (`metacomputer who`). The runner
  checks before every task and stops if another session is active.
- **The VM is checked again before every reset.** The runner refuses the VM your
  own `config/local.env` names, as well as `metacomputer`, unless you pass
  `--allow-main-vm`. It sets `MCOMP_EXPECT_VM` to the VM it found, so every
  `metacomputer` command it starts refuses if the settings later name another VM.
  `metacomputer learn bench` also passes what you confirmed (`BENCH_EXPECT_VM`,
  `BENCH_EXPECT_BASELINE`, `BENCH_EXPECT_CONFIG_SHA`), and any change to them stops
  the run before a reset.
- **Unknown costs count.** A session that ends without reporting its cost (killed
  at its timeout, crashed) is counted at its whole budget against the cap
  (`cost_unknown` in its record). Each task's budget is reserved in `run.json`
  (`reservations`, each with its own id) before its session starts, and only the
  task's record settles it: a reservation left by a runner that died stays and is
  charged on every later resume. `--budget` and `--cap` must be positive amounts.
- **A reset checks what it replaces.** The runner also sets `MCOMP_EXPECT_BASELINE`
  and `MCOMP_EXPECT_CONFIG_SHA`, and `metacomputer reset` compares them, and the VM
  name, with the values it is about to use, in its own process and again right
  before the copy.
- **Keep the VM's optional hardening as you mean to measure it.** A test VM's
  settings often turn on `ACTION_LOG`, which puts a relay between the bridge and
  the driver. For a baseline of the defaults, point `MCOMP_CONFIG` at a small
  file that sources the test VM's settings and sets `ACTION_LOG=off` (it's
  host-side only). `run.json` records what was on (`vm_options`).
- **Calc:** VMs built since 2026-11-18 have Calc, and the Calc tasks use it directly
  (`calc: system`). On an older VM without it, setup for the five Calc tasks does three things (`guest/calc-overlay.sh`,
  about 5 s, all removed by the next reset):
  - it builds a user-level copy of LibreOffice with Calc added, from
    `libreoffice-calc` packages that match the VM's LibreOffice version (cached
    in `results/bench/cache/lodeb/`, or downloaded by the VM);
  - it adds menu entries for the copy;
  - it starts the copy with no window. The driver launches apps by name with the
    system's PATH, so `libreoffice --calc FILE` runs the system LibreOffice,
    which has no Calc. LibreOffice hands every launch to the instance already
    running for the same profile, so with the copy running, Calc opens.

  The agent sees an ordinary Calc window. One difference remains: the
  `launch_app` result names a launcher process that exits, and the window
  belongs to the copy's process. Each result records `calc: system` or
  `calc: overlay`. Compare runs only when both have the same value.
- **Disk:** a run keeps little. Per task there's a gzipped transcript with images
  removed and long texts clipped (tens of KB), plus `result.json`. Nothing is
  recorded.

## Running it

```bash
export MCOMP_CONFIG=/path/to/test-vm.env        # the test VM's settings
bench/bench.py list                             # the tasks
bench/bench.py run --tasks ff-form --run-id try-1          # one task
bench/bench.py run --run-id baseline-2026-11-17            # all 20
bench/bench.py report baseline-2026-11-17                  # rewrite summary.md
bench/bench.py compare baseline-2026-11-17 shell-forbid-1  # A vs B, per task and in total
bench/bench.py check ff-form [--setup]          # run a task's setup and/or check alone, no model
bench/bench.py remetric baseline-2026-11-17     # recompute metrics from the saved transcripts
```

`run` stops the VM at the end unless you pass `--keep-running`.

A task whose VM didn't start or whose setup failed is retried once, then
recorded as an infrastructure failure. A session that ends on an API error
(a usage or rate limit, at no cost) is recorded the same way, and the run stops
there, since every later task would fail the same way.

Running the same `--run-id` again resumes the run:
- it skips the tasks already done;
- it retries the ones that failed for infrastructure reasons;
- it counts what the run has already spent against the cap.

The summary shows the latest attempt of each task and lists the retries.

Results go to `results/bench/<run-id>/`, or to `$BENCH_RESULTS/<run-id>/`:
- `run.json`: the switches, the git sha, the model and the Claude Code version;
- `results.jsonl`: one line per task;
- `summary.md`;
- per task `<task>/result.json` and `<task>/transcript.jsonl.gz`. Each event in
  the transcript carries `t`, its arrival time in seconds, so the time spent
  waiting on VM tools shows.

## Switches

Change one at a time. Each switch is a flag or an environment variable, and
every result records them all, with the metacomputer git sha, the driver
version and the model.

| Flag | Variable | Default | What it changes |
| --- | --- | --- | --- |
| `--model` | `BENCH_MODEL` | `claude-opus-5-5` | The model |
| `--skill DIR` | `BENCH_SKILL` | this checkout's `skills/metacomputer` | The skill the session gets (copied in as a project skill; its hash is recorded) |
| `--shell allow\|forbid` | `BENCH_SHELL` | `allow` | `forbid` denies `metacomputer ssh` and `metacomputer sh` through the session's permissions and asks in the prompt to work through the desktop. This is the placeholder for comparing with and without the coming `metacomputer sh` |
| `--coord-guard on\|off` | `BENCH_COORD_GUARD` | `off` | Passed to the bridge as `MCOMP_COORD_GUARD`, which overrides the VM config's `COORD_GUARD`: the hub's coordinate guard (pixel input refused when the windows changed since its screenshot). `run.json` records whether this checkout has it (`coord_guard_in_bridge`) |
| `--hub on\|off` | `BENCH_HUB` | the VM config's `MCP_HUB` | Passed as `MCOMP_MCP_HUB`: `on`, sessions reach the VM through the shared hub; `off`, each bridge has its own connection (the same guards) |
| `--screen WxH` | `BENCH_SCREEN` | the VM's own (1920x1080) | Set with `xrandr` after each reset, e.g. `1280x800` or `1600x900`; the result records what the VM reports |
| `--effort` | `BENCH_EFFORT` | the model's default | Claude Code's effort level |
| `--budget USD` | `BENCH_BUDGET` | `3` | Per-task cap (`--max-budget-usd`); a task file can set `budget_usd` |
| `--cap USD` | `BENCH_CAP` | `60` | The whole run's cap: a task doesn't start if its budget could take the total over |
| `--reset each\|none` | `BENCH_RESET` | `each` | Reset the VM to its baseline before every task |
| `--lessons on\|off` | `BENCH_LESSONS` | `off` | `on`: the sessions' MCP instructions carry the lessons approved with `metacomputer learn` (from `MCOMP_LESSONS_DIR`, else the default store), passed as `MCOMP_LESSONS`; `run.json` records how many and a hash of the exact text (`lessons_count`, `lessons_hash`). `metacomputer learn bench` runs off, then on, and compares them (docs/learn.md) |

On screen size: the VM's display device offers many modes (from 1280x800 up to
4096x2160), and `xrandr` switches between them while the VM runs. The desktop
and the driver follow without a restart, so there's no need to rebuild the VM
(`VM_DISPLAY` only sets the starting size at build time).

### How a session is set up

Every session gets the same harness, so only the switches differ:
- `claude -p` runs with `env -u CLAUDECODE`, in a folder outside any
  repository (a fresh private folder from `mkdtemp` in your own temporary folder,
  removed afterwards; `claude` is the absolute path found on PATH before the
  shim folder for `metacomputer` goes in front), with `--setting-sources
  project`. Your user settings, plugins, hooks, global CLAUDE.md and user
  skills are left out, and the only skill is the one under test.
- The `metacomputer` MCP server (`mcp-lazy`) points at the test VM.
- Allowed tools: `Skill`, `Read`, `Glob`, `Grep`, `ToolSearch`, the
  `metacomputer` MCP tools, and Bash for the `metacomputer` command only.
- Limits: `--max-turns` (60 by default), `--max-budget-usd` and a wall-clock
  timeout (the task's `timeout_s`; the process group is stopped when it runs out).

The skill text names the installed launcher (`~/.local/bin/metacomputer`).
The copy given to the session names this checkout's `bin/metacomputer`
instead, so a branch is tested with its own CLI. `metacomputer` and `mcomp`
on the session's PATH point there too.

Every prompt starts with the same line:

> Use your virtual computer (the metacomputer) for this. Leave the virtual
> computer running when you finish; no recording is needed.

## The tasks

`route` is where we expect the best route to be:
- `ax`: the accessibility tree;
- `shell`: a shell;
- `visual`: only a screenshot can answer;
- `mixed`: a combination.

| Task | App | Route | What it tests | Its check |
| --- | --- | --- | --- | --- |
| calc-arith | GNOME Calculator | ax | 4817 × 263 − 1094 with the calculator's buttons, left on the display | The display (copied with xdotool and xclip) reads 1265777 |
| mousepad-edit | Mousepad | ax | Change one line, add a last line, save | The file: the line changed, the line added, everything else unchanged |
| mousepad-new | Mousepad | ax | A new document saved into a new folder | The file has exactly the three lines |
| mousepad-prefs | Mousepad | ax | Preferences: line numbers on, tab width 4 | `gsettings` values |
| writer-format | Writer | ax | A Heading 1 and a paragraph with one bold word, saved as .odt | `content.xml`: the outline level, the text, which runs are bold |
| writer-docx | Writer | ax | Replace all, then save a copy as .docx | `word/document.xml`: four "Northwind" and no "ACME" |
| calc-formula | Calc | ax | Row formulas and a SUM, saved as .ods | Each cell's value and that it's a formula |
| calc-sort | Calc | ax | Sort by a date column with the header kept | Row order, and every row's cells still together |
| calc-chart | Calc | mixed | Insert a column chart | An embedded chart object of class bar that uses the Visitors column |
| calc-csv | Calc | ax | Export a sheet as CSV | The CSV's header and rows |
| multi-calc-writer | Calc + Writer | mixed | A SUM in Calc, then the total written into a new Writer document | The .odt's text has the sentence and 5486.45 |
| thunar-organize | Thunar | ax | New folder, move three files into it, rename one | The file tree |
| thunar-copy | Thunar | ax | Copy a folder elsewhere, delete another | The file tree, with contents |
| ff-form | Firefox | ax | A sign-up form: text, email, a select and a checkbox | The server log of the POST, and its user agent |
| ff-read | Firefox | ax | Read a value from a table, enter it on another page | The submitted total |
| ff-canvas-code | Firefox | visual | A code drawn on a canvas only, typed into a box | The submitted code |
| ff-pick-color | Firefox | visual | Click the green one of three unlabeled tiles | Which tile the browser requested |
| chrome-nav | Chrome | ax | Follow links to a product page and press Reserve | The reserve request, its referrer chain and Chrome's user agent |
| chrome-form | Chrome | ax | A shipping form: a select, radio buttons and a checkbox | The server log of the POST |
| data-transform | (any) | shell | Per-category totals from a 240-row CSV | The output CSV: header, categories, totals, order |

All fixture data is invented: names, shops, figures and `example.com` addresses.
The web pages are served from inside the VM on `127.0.0.1:8000` by
`guest/server.py`, so no task needs the internet. The server and its log live in
`~/.cache/bench-srv` in the VM, away from the task folders in `~/bench`.

## Adding a task

Add `bench/tasks/<id>.toml`:

```toml
id = "mousepad-new"
title = "Mousepad: new file, three lines, Save As"
apps = ["mousepad"]
route = "ax"                 # ax | shell | visual | mixed
requires = []                # ["calc"] for LibreOffice Calc
files = ["notes"]            # from bench/fixtures/files/, copied into ~/bench/ in the VM
serve = false                # true: serve bench/fixtures/site on 127.0.0.1:8000
timeout_s = 600              # optional; budget_usd and max_turns too
setup = """                  # optional bash, run in the VM after the files are copied
to_odf ods ~/bench/x.csv ~/bench/sheets     # helper: convert with a separate LibreOffice profile
"""
prompt = """
In Mousepad, create a new document ...
"""
check = """                  # Python run in the VM after the session; checklib is imported
t = read("~/bench/out/packing-list.txt")
C.add("file saved", 0.3, t is not None)
C.add("exactly the three lines", 0.7, ...)
"""
```

Each check adds weighted items with `C.add(name, weight, ok, detail)`. The task
passes when every item is met; its partial score is the weighted share met.
`guest/checklib.py` has the readers:
- files and numbers: `read`, `num`;
- the server log: `requests_log`, `field`;
- ODF: `odt_blocks`, `bold_runs` (what shows in bold, span overrides included; tested by `tests/test_bench_checks.py`), `ods_rows`, `cell`, `cval`, `ods_charts`;
- other files: `docx_text`, `csv_rows`;
- the desktop: `gsetting`, `windows`, `copy_from_window`.

It reaches the VM only after the session has ended.

Try a new check without a model: `bench/bench.py check <id> --setup`, then make
the expected change in the VM by hand (`metacomputer view`), then run
`bench/bench.py check <id>` again.

## Comparing runs

`compare A B` names what changed between the two runs (switches and git sha),
then gives:
- a table per task: pass, partial score, cost, turns, minutes, screenshots,
  tree reads, shell calls, refused and invalid calls;
- totals over the tasks both runs have.

A single run of each task is noisy. For a decision, run the same switch two or
three times and compare the totals.

## Costs

Each task is capped at $3 by default, and the run at $60.

The first baseline (2026-11-17, Opus 5.5, Cua Driver 0.52.1, the default skill)
passed all 20 tasks:
- it cost $11.95 in all, from $0.20 to $1.31 per task, $0.60 on average;
- a session took 20 s to 2.3 min, 0.9 min on average;
- with about 25 s per task for the reset and setup, a full run takes about 35
  minutes.

Its numbers are in `results/bench/baseline-2026-11-17/summary.md`. Because every
task passed, later changes will show up mostly in cost, turns, time and the
tool-call mix. Make tasks harder (or add some) when you need pass rates to
separate two variants.

The hub and the coordinate guard (2026-11-18, mcsecrets with native Calc,
`ACTION_LOG=off`, the same skill, budget $2 a task) changed nothing measurable:

| Run | Passed | Cost | Mean turns | Mean minutes | Screenshots | Refused |
| --- | --- | --- | --- | --- | --- | --- |
| `hub-baseline-2026-11-18`: `--hub off --coord-guard off` | 20/20 | $11.80 | 20.1 | 0.9 | 25 | 0 |
| `hub-on-2026-11-18`: `--hub on --coord-guard on` | 20/20 | $11.83 | 20.2 | 0.9 | 25 | 0 |

One hub served all 20 sessions and reconnected after each task's reset, and the
coordinate guard refused nothing: in these tasks no window changed between a
screenshot and the pixels aimed from it. Per-task differences are within one
run's noise.

Cua Driver 0.53.0 (2026-11-18, test VM mcdrv built from the update's branch,
the same switches as `hub-on`, the skill from main at the time) also passed all 20:

| Run | Passed | Cost | Mean turns | Mean minutes | Screenshots | Refused | Invalid |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `hub-on-2026-11-18` (driver 0.52.1) | 20/20 | $11.83 | 20.2 | 0.9 | 25 | 0 | 14 |
| `driver-053-2026-11-18` (driver 0.53.0) | 20/20 | $11.49 | 19.1 | 1.0 | 39 | 0 | 9 |

The driver's own time fell from 1.18 to 1.00 s per tool call. Sessions ran a
little longer because the model's own time rose, not the VM's. The extra
screenshots come from the skill: main's skill had gained a section on pixels
since `hub-on` (screenshot-only reads before pixel clicks), so this comparison
is not of the driver alone.

Lessons from `metacomputer learn` (2026-11-18, a test VM with 0.53.0, budget $1.50
a task, `learn bench` with two approved built-in lessons, `no-frame` and `check-text`):

| Run | Passed | Cost | Mean turns | Mean minutes | Screenshots | Invalid | Other errors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `learn-2026-11-18-without` (`--lessons off`) | 19/20 | $9.67 | 17.1 | 0.86 | 30 | 0 | 12 |
| `learn-2026-11-18-with` (`--lessons on`) | 19/20 | $9.81 | 16.6 | 0.86 | 29 | 0 | 9 |

The same task (writer-format) failed at the same check in both. The two
lessons' patterns didn't come up without them either, so the differences are
noise; see docs/learn.md.

A session's claims go with that session: Claude Code is recognized however it's
installed, including the native installer's `~/.local/share/claude/versions/X.Y.Z`
(before 2026-11-18 that one wasn't, so a run started from inside Claude Code could
stop with "another controller is using …" after a task that used `metacomputer sh`).
If a run stops on another controller anyway, `metacomputer who` names it; run the
same `--run-id` again once it's idle.
