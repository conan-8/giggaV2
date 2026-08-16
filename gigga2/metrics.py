"""Instrumentation (master plan §9).

Per-run metrics land in <run>/metrics.json; one summary line per run is
appended to ~/.gigga2/metrics.jsonl so cross-run rates (fork_rate,
fasttrack_escalation_rate, cache_hit_rate, stage medians) can be computed.

Automatic flags: any stage over 2x the median for its type; any chain
replanned more than once; any prompt executed more than twice; any gate that
passed with zero checks configured.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .config import METRICS_LOG


def compute_metrics(state) -> dict:
    s = state.state
    events = state.read_events()
    counts: dict[str, int] = {}
    for ev in events:
        counts[ev["event"]] = counts.get(ev["event"], 0) + 1

    chains = s.get("chains", {})
    replans = {k.split(":", 1)[1]: v for k, v in s.get("counters", {}).items()
               if k.startswith("replan:")}
    execs = {k.split(":", 1)[1]: v for k, v in s.get("counters", {}).items()
             if k.startswith("exec:")}

    metrics = {
        "run_id": s.get("run_id"),
        "terminal": s.get("terminal"),
        "path": s.get("path"),
        "classification": s.get("classification"),
        "fasttrack_escalations": s.get("counters", {}).get("fasttrack_escalations", 0),
        "fasttrack_rejects": s.get("counters", {}).get("fasttrack_rejects", 0),
        "cache_hits": counts.get("cache_hit", 0),
        "coverage_gaps_found": counts.get("coverage_gap_found", 0),
        "kickback_rounds": s.get("counters", {}).get("kickback_rounds", 0),
        "plan_review_rejects": s.get("counters", {}).get("plan_review_rejects", 0),
        "judge_plan_rejects": s.get("counters", {}).get("judge_plan_rejects", 0),
        "replan_count": sum(replans.values()),
        "prompt_count": next((ev["data"]["count"] for ev in reversed(events)
                              if ev["event"] == "prompt_count"), None),
        "judge_verdicts": [v.get("verdict") for v in s.get("verdicts", [])
                           if v.get("stage") == "JUDGE"],
        "halt": next((ev["data"] for ev in reversed(events)
                      if ev["event"] == "halt"), None),
        "wall_clock_machine": {k: round(v.get("machine_s", 0), 1)
                               for k, v in s.get("timing", {}).items()},
        "wall_clock_human": {k: round(v.get("human_s", 0), 1)
                             for k, v in s.get("timing", {}).items()
                             if v.get("human_s")},
        "tokens": s.get("tokens", {}),
    }

    # automatic flags
    flags = []
    for cid, n in replans.items():
        if n > 1:
            flags.append(f"chain {cid} replanned {n} times")
    for pid, n in execs.items():
        if n > 2:
            flags.append(f"prompt {pid} executed {n} times")
    if s.get("checks_empty"):
        flags.append("gate passed with ZERO checks configured")
    metrics["flags"] = flags
    metrics["stage_median_flags"] = _median_flags(s.get("timing", {}))
    return metrics


def _median_flags(timing: dict) -> list[str]:
    """Any stage over 2x the median for its type (across recorded runs)."""
    if not METRICS_LOG.exists():
        return []
    history: dict[str, list[float]] = {}
    try:
        for line in METRICS_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for stage, secs in (rec.get("wall_clock_machine") or {}).items():
                history.setdefault(stage, []).append(secs)
    except OSError:
        return []
    flags = []
    for stage, t in timing.items():
        hist = history.get(stage, [])
        cur = t.get("machine_s", 0)
        if len(hist) >= 3 and cur > 2 * statistics.median(hist) and cur > 30:
            flags.append(f"stage {stage} at {cur:.0f}s is over 2x median "
                         f"({statistics.median(hist):.0f}s, n={len(hist)})")
    return flags


def finalize_metrics(state) -> dict:
    m = compute_metrics(state)
    out = state.run_dir / "metrics.json"
    out.write_text(json.dumps(m, indent=2, default=str), encoding="utf-8")
    try:
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(m, default=str) + "\n")
    except OSError:
        pass
    return m


def render_timeline(state) -> str:
    """One line per journal event with elapsed offsets (§9)."""
    events = state.read_events()
    if not events:
        return "(empty journal)"
    t0 = events[0].get("t", time.time())
    lines = []
    for ev in events:
        off = ev.get("t", t0) - t0
        kind = ev["event"]
        data = ev.get("data", {})
        detail = ""
        if kind == "stage_started":
            detail = data.get("stage", "")
        elif kind == "agent_finished":
            detail = (f"{data.get('name')} tier={data.get('tier')} "
                      f"wall={data.get('wall_s')}s")
        elif kind == "agent_dispatched":
            detail = f"{data.get('name')} tier={data.get('tier')}"
        elif kind == "set":
            interesting = {k: v for k, v in data.items()
                           if k in ("phase", "path", "terminal", "classification")}
            detail = " ".join(f"{k}={v}" for k, v in interesting.items())
        elif kind in ("triage", "classification", "merge", "baseline_delta",
                      "halt", "coverage_gap_found", "divergence", "replan",
                      "preflight_stale", "prompt_count", "warning", "cache_hit",
                      "chain_gate"):
            detail = json.dumps(data, default=str)[:160]
        elif kind == "chain":
            upd = data.get("update", {})
            detail = f"{data.get('id')} " + " ".join(
                f"{k}={v}" for k, v in upd.items() if k in ("status", "reason"))
        elif kind == "validate_plan_failed":
            detail = "; ".join(data.get("errors", [])[:2])[:160]
        lines.append(f"+{off:9.1f}s  {kind:<22} {detail}")
    return "\n".join(lines)


def rates() -> dict:
    """Cross-run rates from the metrics log (§9)."""
    if not METRICS_LOG.exists():
        return {}
    runs = []
    for line in METRICS_LOG.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not runs:
        return {}
    n = len(runs)
    ft = [r for r in runs if r.get("path") == "fasttrack"]
    return {
        "runs": n,
        "fork_rate": sum(1 for r in runs
                         if r.get("classification") == "discovery") / n,
        "fasttrack_rate": len(ft) / n,
        "fasttrack_escalation_rate": (
            sum(1 for r in ft if r.get("fasttrack_escalations")) / len(ft)) if ft else None,
        "cache_hit_rate": sum(1 for r in runs if r.get("cache_hits")) / n,
        "halt_rate": sum(1 for r in runs if r.get("terminal") == "HALT") / n,
    }
