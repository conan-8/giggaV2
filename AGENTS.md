# AGENTS.md — GIGGA v2

## What this is

An agent pipeline for hard software engineering work in existing repositories
(see `docs/gigga-v2-master-plan.md` — the implementation specification; section
numbers below refer to it). Input: a task + a repo. Output: a reviewable git
branch. **Research once. Execute mechanically.**

## Layout

```
gigga2/
  cli.py        entry points: start/resume/status/timeline/rollback/validate-plan/rates
  stages.py     the pipeline state machine (all stages, paths, loop caps)
  prompts.py    agent prompt templates — verbatim load-bearing rules live here
  agents.py     opencode dispatch (tiers, verdict parsing, stall/wall bounds)
  planmodel.py  data model (Prompt/Decision/Chain/Plan) + computer validators (§3)
  journal.py    journal.jsonl (authoritative) + state.json (derived cache) (§3)
  probe.py      deterministic repo probe → probe.md + probe.json (§4.0)
  checks.py     check ladder, baseline delta, test impact selection (§4.0, §5.2)
  cache.py      artifact cache keyed by baseline_sha (§5.1)
  worktrees.py  worktree/branch lifecycle + rollback (§4.7, §4.10)
  repo.py       git helpers, cite anchor resolution + hashing (§4.7a)
  report.py     APPLY and HALT reports (§4.10, §4.HALT)
  metrics.py    instrumentation + timeline (§9)
  config.py     ~/.gigga2 config, model tiers (§8)
  installer.py  opencode integration: gigga2 install/uninstall
  assets/       GIGGA.md (red primary agent) + gigga-flow.tsx (sidebar flowchart)
eval/           eval harness + task packs (§10)
docs/           master plan + model allocation rationale (§8)
```

## Invariants — do not break these

- **Field names in `planmodel.py` are contractual.** Every stage assumes them.
- **Only the computer blocks.** Agents emit verdicts; the runner enforces. Never
  trust an agent's self-reported success.
- **All three paths pass the check ladder, baseline delta, and judge.** No
  exceptions (§5.6). Efficiency means removing waste, never removing verification.
- **No brevity directives** in agent prompts for artifacts or inter-agent replies (§8).
- **Never touch the user's working tree.** All edits happen in dedicated
  worktrees; run directories live outside the target repo
  (`~/.gigga2/runs/...`). `journal.jsonl` is authoritative — `state.json` must
  reconstruct identically by replay.
- **Executor prompts: stable prefix + varying tail** (§5.3). Never interleave.
- opencode's `-f` is a greedy yargs array: the message positional must come
  **before** any flags in `agents.py`.

## Conventions

- Python 3.9+, **stdlib only**, pathlib everywhere, works on Windows + POSIX.
- No third-party deps in the package; agents are dispatched via the opencode CLI.
- Loop caps and model tiers are configuration (`config.py`), not literals
  scattered through stage code.

## Testing

- Foundations (probe/checks/validators/journal/worktrees) have no unit-test suite;
  verify changes with a scratch git repo and direct module calls.
- End-to-end: run a trivial task (fasttrack) and a multi-module task (full path)
  against a scratch repo: `python -m gigga2 start --repo <scratch> --request "..." --non-interactive`.
  Checkpoints per §12: you should get a branch, a diffstat, and a clean delta.
