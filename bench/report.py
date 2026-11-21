"""Markdown reports for benchmark runs: one run's summary, and two runs compared
per task and in total (HCU-style tables). Reads results/bench/<run>/results.jsonl.

Each task's saved transcript also goes through `metacomputer learn`'s classifier (lib/learn_mine.py), so a
report counts the failure patterns the lessons are about: failed driver calls by class (no_frame, not_found,
check_failed, ...), `churn` (runs of three or more routes after a failure, switched without re-checking),
`repeat` (times a failure class came back within the session) and `acted_without_check` (an unverified
result acted on without a check). A task can name the patterns it was built to bring up (`targets`, the
lessons set); those are its "targeted" count."""
import collections
import json
import os
import re
import sys

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
API_FAIL = re.compile(r"hit your (weekly |daily |session )?limit|usage limit|rate limit|overloaded|credit balance|"
                      r"API Error|authentication", re.I)
BEHAVIOUR = ("churn", "repeat", "acted_without_check")


def patterns_of(rdir, task):
    """{pattern: count} from learn's classifier over a task's saved transcript (the latest attempt's), or None
    when there's none or it can't be read. Failure classes count failed driver calls; `churn`, `repeat` and
    `acted_without_check` are the behaviour patterns (module docstring)."""
    p = os.path.join(rdir, task, "transcript.jsonl.gz")
    if not os.path.isfile(p):
        return None
    try:
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        import learn_mine
        import transcripts
        m = learn_mine.mine_transcript(transcripts.parse(p, slim=True), "bench", task)
    except Exception:
        return None
    out = {k: v for k, v in m["classes"].items() if v}
    rep = sum(n - 1 for n in m["repeats"].values())
    for k, v in (("churn", len(m["churn"])), ("repeat", rep), ("acted_without_check", m["acted_without_check"])):
        if v:
            out[k] = v
    return out


def pattern_label(k):
    try:
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        import learn_mine
        labels = dict(learn_mine.LABELS)
    except Exception:
        labels = {}
    labels.update(churn="routes changed without re-checking (runs of 3+)", repeat="a failure class came back",
                  acted_without_check="acted again after an unverified result, without a check")
    return labels.get(k, k)


def infra_reason(rec):
    """Why a task's record isn't a real attempt (the runner retries it on resume), or None."""
    if rec.get("infra_failure"):
        return rec["infra_failure"] if isinstance(rec["infra_failure"], str) else "setup failed"
    m = rec.get("metrics") or {}
    if m.get("is_error") and not (m.get("cost_usd") or 0) and (
            m.get("api_error_status") or API_FAIL.search(m.get("final_text") or "")):
        return f"api error {m.get('api_error_status') or ''}: {(m.get('final_text') or '')[:120]}".replace("  ", " ")
    return None


def load_all(rdir):
    """Every record in results.jsonl, retried tasks included."""
    p = os.path.join(rdir, "results.jsonl")
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []


def latest(recs):
    """The last record of each task (a resumed run appends retries), in first-seen order;
    each carries `earlier_attempts`: the infrastructure failures it replaced."""
    by, order = {}, []
    for r in recs:
        if r["task"] not in by:
            order.append(r["task"])
            by[r["task"]] = []
        by[r["task"]].append(r)
    out = []
    for t in order:
        r = dict(by[t][-1])
        r["earlier_attempts"] = [infra_reason(x) or "earlier attempt" for x in by[t][:-1]]
        out.append(r)
    return out


def load(rdir):
    meta = json.load(open(os.path.join(rdir, "run.json")))
    recs = latest(load_all(rdir))
    order = {t: i for i, t in enumerate(meta.get("tasks") or [])}
    recs.sort(key=lambda r: order.get(r["task"], len(order)))
    for r in recs:
        r["patterns"] = patterns_of(rdir, r["task"]) if r.get("session") else None
    return meta, recs


def _m(r, k, default=0):
    v = (r.get("metrics") or {}).get(k)
    return default if v is None else v


def row(r):
    m = r.get("metrics") or {}
    a = m.get("actions") or {}
    return {
        "task": r["task"], "route": r.get("route") or "",
        "pass": bool((r.get("check") or {}).get("pass")), "score": float((r.get("check") or {}).get("score") or 0),
        "cost": float(m.get("cost_usd") or 0), "turns": int(m.get("turns") or 0),
        "min": float((r.get("session") or {}).get("wall_s") or 0) / 60,
        "shots": int(m.get("screenshots") or 0), "tree": int(m.get("tree_reads") or 0),
        "ax": int(a.get("ax", 0)), "pixel": int(a.get("pixel", 0)), "kbd": int(a.get("keyboard", 0)),
        "page": int(a.get("page", 0)), "steps": int(m.get("run_steps") or 0),
        "shell": int(m.get("shell_calls") or 0), "cli": sum((m.get("cli_calls") or {}).values()),
        "refused": int(m.get("refused_bridge") or 0) + int(m.get("refused_driver") or 0) + int(m.get("denied_permission") or 0),
        "invalid": int(m.get("invalid_args") or 0), "errors": int(m.get("errors_other") or 0),
        "tool_calls": int(m.get("tool_calls") or 0),
        "in_tok": int(m.get("input_tokens") or 0), "out_tok": int(m.get("output_tokens") or 0),
        "timeout": bool((r.get("session") or {}).get("timed_out")), "end": m.get("result_subtype") or "",
        "infra": bool(r.get("infra_failure")),
        "patterns": dict(r.get("patterns") or {}), "targets": list(r.get("targets") or []),
        "targeted": sum((r.get("patterns") or {}).get(k, 0) for k in r.get("targets") or []),
        "fails": sum(v for k, v in (r.get("patterns") or {}).items() if k not in BEHAVIOUR),
    }


def pattern_totals(rows):
    """{pattern: count} over rows, and {pattern: number of tasks that target it}."""
    seen, targeted_by = collections.Counter(), collections.Counter()
    for r in rows:
        seen.update(r.get("patterns") or {})
        targeted_by.update(set(r.get("targets") or []))
    return dict(seen), dict(targeted_by)


def totals(rows):
    n = len(rows) or 1
    s = lambda k: sum(r[k] for r in rows)  # noqa: E731
    return {"n": len(rows), "passed": sum(1 for r in rows if r["pass"]), "pass_rate": sum(1 for r in rows if r["pass"]) / n,
            "targeted": sum(r.get("targeted", 0) for r in rows), "fails": sum(r.get("fails", 0) for r in rows),
            "score": s("score") / n, "cost": s("cost"), "cost_mean": s("cost") / n, "turns_mean": s("turns") / n,
            "min_total": s("min"), "min_mean": s("min") / n, "shots": s("shots"), "tree": s("tree"), "ax": s("ax"),
            "pixel": s("pixel"), "kbd": s("kbd"), "page": s("page"), "steps": s("steps"), "shell": s("shell"),
            "cli": s("cli"), "refused": s("refused"), "invalid": s("invalid"), "errors": s("errors"),
            "tool_calls": s("tool_calls"), "in_tok": s("in_tok"), "out_tok": s("out_tok"),
            "invalid_share": s("invalid") / max(1, s("tool_calls")), "refused_share": s("refused") / max(1, s("tool_calls"))}


def header(meta):
    sw = meta.get("switches") or {}
    g = meta.get("metacomputer") or {}
    return (f"- metacomputer `{(g.get('sha') or '')[:10]}` ({g.get('branch')}{', uncommitted changes' if g.get('dirty') else ''}); "
            f"VM `{meta.get('vm')}` (reset to `{meta.get('baseline')}` before each task: {sw.get('reset')})\n"
            f"- model `{sw.get('model')}`, effort {sw.get('effort') or 'default'}; {meta.get('claude_code')}\n"
            f"- skill `{sw.get('skill')}` (hash {sw.get('skill_hash')}); shell {sw.get('shell')}; "
            f"coordinate guard {sw.get('coord_guard')}{'' if sw.get('coord_guard_in_bridge') else ' (the bridge does not read it yet)'}; "
            f"hub {sw.get('hub') or 'as configured'}; "
            f"screen {sw.get('screen') or 'VM default'}; "
            f"lessons {sw.get('lessons') or 'off'}"
            + (f" ({sw.get('lessons_count')}, hash {sw.get('lessons_hash')})" if sw.get("lessons_hash") else "") + "\n"
            f"- budget ${sw.get('budget')} per task (task files may differ), cap ${sw.get('cap')}; "
            f"spent ${meta.get('spent_usd', 0):.2f}; {meta.get('started')} to {meta.get('finished', '?')}"
            + (f" (resumed {', '.join(meta['resumed'])})" if meta.get("resumed") else "")
            + (f"\n- resumed on metacomputer `{((meta['resume_note'].get('earlier_metacomputer') or {}).get('sha') or '')[:10]}` before; "
               "check that the code under test didn't change between the two" if meta.get("resume_note") else "")
            + (f"\n- stopped early: {meta['stopped']}" if meta.get("stopped") else ""))


def write_summary(rdir):
    meta, recs = load(rdir)
    rows = [row(r) for r in recs]
    env = (recs[0].get("env") or {}) if recs else {}
    calcs = sorted({str((r.get("env") or {}).get("calc")) for r in recs if (r.get("env") or {}).get("calc")})
    out = [f"# Benchmark run {meta['run_id']}", "", header(meta),
           f"- driver {env.get('driver_version')}, screen {env.get('resolution')}, Calc: {', '.join(calcs) or 'n/a'}"
           if recs else "", "",
           "| Task | Route | Pass | Partial | Cost $ | Turns | Min | Screenshots | Tree reads | AX acts | Pixel acts | Keys | Page acts | run_steps | Shell | Refused | Invalid | Other errors |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        flag = " (timeout)" if r["timeout"] else (" (infra)" if r["infra"] else (f" ({r['end']})" if r["end"] not in ("success", "") else ""))
        out.append(f"| {r['task']} | {r['route']} | {'yes' if r['pass'] else 'no'}{flag} | {r['score']:.2f} | {r['cost']:.2f} | "
                   f"{r['turns']} | {r['min']:.1f} | {r['shots']} | {r['tree']} | {r['ax']} | {r['pixel']} | {r['kbd']} | "
                   f"{r['page']} | {r['steps']} | {r['shell']} | {r['refused']} | {r['invalid']} | {r['errors']} |")
    if rows:
        t = totals(rows)
        out.append(f"| **total** | | **{t['passed']}/{t['n']}** | **{t['score']:.2f}** | **{t['cost']:.2f}** | "
                   f"{t['turns_mean']:.1f} avg | {t['min_total']:.1f} | {t['shots']} | {t['tree']} | {t['ax']} | {t['pixel']} | "
                   f"{t['kbd']} | {t['page']} | {t['steps']} | {t['shell']} | {t['refused']} | {t['invalid']} | {t['errors']} |")
        out += ["", f"Means per task: cost ${t['cost_mean']:.2f}, {t['turns_mean']:.1f} turns, {t['min_mean']:.1f} min, "
                f"{t['shots'] / t['n']:.1f} screenshots, {t['tree'] / t['n']:.1f} tree reads, {t['tool_calls'] / t['n']:.1f} tool calls. "
                f"Tokens: {t['in_tok']:,} input (incl. cache), {t['out_tok']:,} output. "
                f"Refused {t['refused_share']:.1%} and invalid-argument {t['invalid_share']:.1%} of {t['tool_calls']} tool calls.", ""]
        by = {}
        for r in rows:
            by.setdefault(r["route"], []).append(r)
        out += ["| Expected best route | Tasks | Passed | Mean partial | Mean cost $ | Mean turns | Screenshots | Shell calls |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for k in sorted(by):
            t = totals(by[k])
            out.append(f"| {k} | {t['n']} | {t['passed']} | {t['score']:.2f} | {t['cost_mean']:.2f} | {t['turns_mean']:.1f} | {t['shots']} | {t['shell']} |")
        seen, targeted_by = pattern_totals(rows)
        if seen or targeted_by:
            out += ["", "Failure patterns, by `metacomputer learn`'s classifier over each task's transcript "
                    f"(targeted: the patterns a task was built to bring up; {totals(rows)['targeted']} in all):", "",
                    "| Pattern | Count | Tasks that target it | Tasks where it came up |", "| --- | --- | --- | --- |"]
            for k in sorted(set(seen) | set(targeted_by), key=lambda k: (-seen.get(k, 0), k)):
                where = ", ".join(r["task"] for r in rows if r["patterns"].get(k))
                out.append(f"| {k}: {pattern_label(k)} | {seen.get(k, 0)} | {targeted_by.get(k, 0)} | {where or '-'} |")
        retried = [(r["task"], r["earlier_attempts"]) for r in recs if r.get("earlier_attempts")]
        if retried:
            out += ["", "Retried after infrastructure failures (the table shows the retry):", ""]
            out += [f"- {task}: {'; '.join(str(x)[:140] for x in why)}" for task, why in retried]
        samples = [(r["task"], s) for r in recs for s in (r.get("metrics") or {}).get("error_samples", [])]
        if samples:
            out += ["", "Error samples (first 160 characters):", ""]
            out += [f"- {task}: {s['tool']} [{s['kind']}] {s['text'].replace('|', '/').replace(chr(10), ' ')}" for task, s in samples[:25]]
    path = os.path.join(rdir, "summary.md")
    open(path, "w").write("\n".join(out) + "\n")
    return path


def _d(a, b, fmt="{:+.2f}"):
    return fmt.format(b - a)


def pattern_table(pa, pb, targeted_by, ta, tb, mb):
    """The failure-pattern lines of a comparison (Markdown), or [] when neither run has any."""
    keys = sorted(set(pa) | set(pb) | set(targeted_by),
                  key=lambda k: (-targeted_by.get(k, 0), -(pa.get(k, 0) + pb.get(k, 0)), k))
    if not keys:
        return []
    out = ["", "Failure patterns, by `metacomputer learn`'s classifier over each task's transcript (the tasks both "
           "runs have):", "", "| Pattern | Tasks that target it | A | B | B − A |", "| --- | --- | --- | --- | --- |"]
    for k in keys:
        a, b = pa.get(k, 0), pb.get(k, 0)
        out.append(f"| {k}: {pattern_label(k)} | {targeted_by.get(k, 0)} | {a} | {b} | {b - a:+d} |")
    out += ["", f"Targeted (each task's own targets, summed): A {ta['targeted']}, B {tb['targeted']} "
            f"({tb['targeted'] - ta['targeted']:+d}). Failed driver calls of any class: A {ta['fails']}, B {tb['fails']}."]
    ids = (mb.get("switches") or {}).get("lessons_ids")
    if ids:
        out.append(f"Lessons B's sessions got: {', '.join(ids)}.")
    return out


def public_summary(rdir):
    """A run's summary for bench/runs/: per task whether it passed, its invalid-argument calls, turns, minutes and
    failure patterns (with its targets, for the lessons set), and the run's date, model, driver version, commit and
    switches. No paths, costs, prompts or transcript text."""
    meta, recs = load(rdir)
    sw, g = meta.get("switches") or {}, meta.get("metacomputer") or {}
    env = next(((r.get("env") or {}) for r in recs if (r.get("env") or {}).get("driver_version")), {})
    rows = [row(r) for r in recs]
    tasks = []
    for r, x in zip(recs, rows):
        t = {"task": x["task"], "pass": x["pass"], "invalid_calls": x["invalid"], "turns": x["turns"],
             "minutes": round(x["min"], 2)}
        if r.get("targets"):
            t.update(targets=x["targets"], targeted=x["targeted"])
        t["patterns"] = dict(sorted(x["patterns"].items()))
        if r.get("infra_failure"):
            t["infra_failure"] = True
        tasks.append(t)
    t = totals(rows)
    seen, _ = pattern_totals(rows)
    return {
        "run_id": meta.get("run_id"), "date": str(meta.get("started") or "")[:10], "agent": sw.get("agent") or "claude", "model": sw.get("model"),
        "cua_driver": (env.get("driver_version") or "").replace("cua-driver ", "") or None,
        "metacomputer": {"commit": g.get("sha"), "uncommitted_changes": bool(g.get("dirty"))},
        "task_set": ", ".join(meta.get("task_sets") or ["standard"]),
        "switches": {"hub": sw.get("hub") or "the VM's setting (MCP_HUB)", "coord_guard": sw.get("coord_guard"),
                     "shell": sw.get("shell"), "lessons": sw.get("lessons"), "lessons_count": sw.get("lessons_count", 0),
                     "lessons_ids": sw.get("lessons_ids") or []},
        "tasks": tasks,
        "totals": {"tasks": t["n"], "passed": t["passed"], "invalid_calls": t["invalid"],
                   "mean_turns": round(t["turns_mean"], 2), "mean_minutes": round(t["min_mean"], 2),
                   "targeted": t["targeted"], "failed_driver_calls": t["fails"],
                   "patterns": dict(sorted(seen.items()))},
    }


def write_compare(ra, rb):
    ma, xa = load(ra)
    mb, xb = load(rb)
    A = {r["task"]: row(r) for r in xa}
    B = {r["task"]: row(r) for r in xb}
    sa, sb = ma.get("switches") or {}, mb.get("switches") or {}
    diffs = [f"`{k}`: {sa.get(k)} → {sb.get(k)}" for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]
    ga, gb = (ma.get("metacomputer") or {}).get("sha", "")[:10], (mb.get("metacomputer") or {}).get("sha", "")[:10]
    if ga != gb:
        diffs.append(f"metacomputer `{ga}` → `{gb}`")
    out = [f"# {ma['run_id']} (A) vs {mb['run_id']} (B)", "",
           "What changed: " + ("; ".join(diffs) if diffs else "nothing recorded (same switches and code)"), "",
           "| Task | Pass A→B | Partial A / B | Cost $ A / B | Turns A / B | Min A / B | Screenshots A / B | Tree reads A / B | Shell A / B | Refused+invalid A / B | Targeted A / B |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    yn = lambda r: ("yes" if r["pass"] else "no") if r else "-"  # noqa: E731
    for k in [t for t in A if t in B] + [t for t in A if t not in B] + [t for t in B if t not in A]:
        a, b = A.get(k), B.get(k)
        f = lambda key, fmt="{}": f"{fmt.format(a[key]) if a else '-'} / {fmt.format(b[key]) if b else '-'}"  # noqa: E731
        ri = f"{(a['refused'] + a['invalid']) if a else '-'} / {(b['refused'] + b['invalid']) if b else '-'}"
        tg = f('targeted') if (a or b or {}).get("targets") else "-"
        out.append(f"| {k} | {yn(a)} → {yn(b)} | {f('score', '{:.2f}')} | {f('cost', '{:.2f}')} | {f('turns')} | "
                   f"{f('min', '{:.1f}')} | {f('shots')} | {f('tree')} | {f('shell')} | {ri} | {tg} |")
    common = [k for k in A if k in B]
    ta, tb = totals([A[k] for k in common]), totals([B[k] for k in common])
    pa, tga = pattern_totals([A[k] for k in common])
    pb, tgb = pattern_totals([B[k] for k in common])
    out += ["", f"Totals over the {len(common)} tasks both runs have:", "",
            "| Metric | A | B | B − A |", "| --- | --- | --- | --- |"]
    for label, key, fmt in [("passed", "passed", "{}"), ("pass rate", "pass_rate", "{:.0%}"), ("mean partial score", "score", "{:.2f}"),
                            ("total cost $", "cost", "{:.2f}"), ("mean cost $", "cost_mean", "{:.2f}"),
                            ("mean turns", "turns_mean", "{:.1f}"), ("mean minutes", "min_mean", "{:.1f}"),
                            ("screenshots", "shots", "{}"), ("tree reads", "tree", "{}"), ("AX actions", "ax", "{}"),
                            ("pixel actions", "pixel", "{}"), ("shell calls", "shell", "{}"), ("tool calls", "tool_calls", "{}"),
                            ("refused calls", "refused", "{}"), ("invalid-argument calls", "invalid", "{}"),
                            ("input tokens", "in_tok", "{:,}"), ("output tokens", "out_tok", "{:,}")]:
        va, vb = ta[key], tb[key]
        delta = ("{:+.0%}" if key == "pass_rate" else "{:+,.2f}" if isinstance(va, float) else "{:+,}").format(vb - va)
        out.append(f"| {label} | {fmt.format(va)} | {fmt.format(vb)} | {delta} |")
    out += pattern_table(pa, pb, dict(tga, **tgb), ta, tb, mb)
    path = os.path.join(os.path.dirname(ra), f"compare-{os.path.basename(ra)}-vs-{os.path.basename(rb)}.md")
    open(path, "w").write("\n".join(out) + "\n")
    return path
