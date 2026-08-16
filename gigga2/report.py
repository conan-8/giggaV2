"""APPLY and HALT reports (master plan §4.10, §4.HALT).

The HALT report must explain the run well enough to act on without reading
the journal. Sections with nothing to report say so explicitly — an absent
section reads as an oversight.
"""

from __future__ import annotations

import json


def _fmt_seconds(x: float) -> str:
    m, s = divmod(int(x), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _timing_block(state) -> str:
    timing = state.state.get("timing", {})
    tokens = state.state.get("tokens", {})
    if not timing:
        return "(no timing recorded)"
    lines = []
    for stage, t in timing.items():
        tk = tokens.get(stage, {})
        tok = ""
        if tk:
            tok = (f" · tokens in={tk.get('input', 0)} out={tk.get('output', 0)}"
                   f" reasoning={tk.get('reasoning', 0)} cache_read={tk.get('cache_read', 0)}")
        lines.append(f"  {stage:<16} machine={_fmt_seconds(t.get('machine_s', 0))}"
                     f" human={_fmt_seconds(t.get('human_s', 0))}{tok}")
    return "\n".join(lines)


def render_apply(pipe) -> str:
    from . import repo as R

    s = pipe.state.state
    run_id = s.get("run_id")
    branch = f"g2/{run_id}/result"
    try:
        stat = R.diffstat(s["repo_path"], s["baseline_sha"], branch) or "(no diff)"
    except Exception:  # noqa: BLE001
        stat = "(diffstat unavailable)"

    checks = pipe.read_artifact("integration-checks.json")
    delta = pipe.read_artifact("delta.json")
    assumptions = s.get("assumptions", [])
    coverage = pipe._coverage_statement()

    return f"""# GIGGA v2 — DONE

**Branch:** `{branch}` (review it; nothing was merged to your working tree)

## Diffstat
```
{stat}
```

## Path taken
{s.get("path") or "?"}

## Check results
```
{_summarize_checks(checks)}
```

## Baseline delta
```
{_summarize_delta(delta)}
```

## [ASSUMPTION] defaults applied without confirmation
{chr(10).join('- ' + a for a in assumptions) if assumptions else '(none)'}

## Coverage statement
{coverage or '(empty — nothing was excluded)'}

## Per-stage wall clock and tokens (human wait reported separately, §5.5)
{_timing_block(pipe.state)}

The assumption list and coverage statement above are your last chance to catch
what every gate missed. `gigga2 rollback --dir {pipe.run_dir}` removes every
branch and worktree this run created.
"""


def _summarize_checks(raw: str) -> str:
    if not raw:
        return "(no check results)"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:2000]
    if data.get("empty"):
        return "CHECK LADDER EMPTY — this run had no objective gate"
    lines = [f"  [{'ok' if c.get('exit') == 0 else 'FAIL'}] {c.get('name')}"
             f" ({c.get('kind')}): exit={c.get('exit')} ({c.get('duration_s', '?')}s)"
             for c in data.get("checks", [])]
    return "\n".join(lines) or "(no checks ran)"


def _summarize_delta(raw: str) -> str:
    if not raw:
        return "(no delta)"
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:2000]
    if d.get("note"):
        return d["note"]
    return "\n".join(
        f"  {k}: {len(v) if isinstance(v, list) else v}"
        + (f" — {v[:5]}" if isinstance(v, list) and v and k in
           ("regression", "new_failing") else "")
        for k, v in d.items() if k != "hard_failure") + \
        f"\n  hard_failure: {d.get('hard_failure')}"


def render_halt(pipe, h) -> str:
    from . import repo as R

    s = pipe.state.state
    run_id = s.get("run_id")
    branch = f"g2/{run_id}/result"
    try:
        stat = R.diffstat(s["repo_path"], s["baseline_sha"], branch) \
            if any(b == branch for b in s.get("branches", [])) else ""
    except Exception:  # noqa: BLE001
        stat = ""
    if not stat:
        # fall back to whatever chain branches exist
        chain_branches = [b for b in s.get("branches", []) if not b.endswith("/result")]
        if chain_branches:
            try:
                stat = R.diffstat(s["repo_path"], s["baseline_sha"], chain_branches[-1])
                branch = chain_branches[-1]
            except Exception:  # noqa: BLE001
                stat = ""

    attempts = s.get("attempts", [])
    verdicts = [v for v in s.get("verdicts", []) if v.get("verdict") not in (None, "PASS", "DONE")]
    if not attempts and verdicts:
        attempts = [f"{v['stage']} {v['verdict']}: {v.get('detail', '')[:200]}"
                    for v in verdicts[-4:]]

    return f"""# FAILED — {h.gate}, at {h.stage}

## What you asked for
{s.get("request") or "(not recorded)"}

## What got built
branch `{branch}` · path taken: {s.get("path") or "?"}
```
{stat or '(nothing landed on a branch)'}
```

## Where it stuck
{h.stuck}

## What was tried
{chr(10).join('- ' + a for a in attempts) if attempts else
 '(nothing to report — the run halted before any sendback loop)'}

## Why it could not resolve
**{h.category}** — {
    're-run with a hint.' if h.category == 'code wrong' else
    'answer a clarify question and re-run.' if h.category == 'plan wrong' else
    'rewrite the task; it is ambiguous.'}

## Where to look
{chr(10).join('- ' + p for p in h.where_to_look) if h.where_to_look else
 f'- run journal: {pipe.run_dir}/journal.jsonl'}

## What would unblock it
{chr(10).join('- ' + u for u in h.unblock) if h.unblock else
 '- inspect the artifacts above; the run state is preserved on disk'}

## Per-stage wall clock and tokens
{_timing_block(pipe.state)}

Everything is preserved: branches, worktrees, partial work. `rollback` stays
manual: `gigga2 rollback --dir {pipe.run_dir}`
"""
