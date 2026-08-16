# GIGGA v2

An agent pipeline for hard software engineering work in existing repositories:
multi-module features, refactors, schema changes, whole project phases.
Input is a task and a repo. Output is a reviewable git branch.

**Governing principle:** one expensive research pass compiles a hard problem into
instructions simple enough to execute cheaply. Research once. Execute mechanically.

## Install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/conan-8/giggaV2/main/install.sh | bash
```

or directly with [uv](https://docs.astral.sh/uv/) / pipx:

```bash
uv tool install git+https://github.com/conan-8/giggaV2.git
```

**Requirements:** Python 3.9+, git, and the [opencode](https://opencode.ai) CLI with
a configured provider (gigga2 dispatches all of its agents through opencode).

## Use

```bash
gigga2 start --repo /path/to/repo --request "Add rate limiting to the public API"
gigga2 start --repo . --request-file task.md --dir runs/my-run
gigga2 resume --dir runs/my-run        # continue an interrupted run
gigga2 status --dir runs/my-run
gigga2 timeline --dir runs/my-run      # journal, one line per event, elapsed offsets
gigga2 rollback --dir runs/my-run      # remove every branch/worktree the run created
gigga2 validate-plan --dir runs/my-run # computer-run plan validators
gigga2 rates                           # cross-run instrumentation (fork/fasttrack/cache rates)
```

A run ends `DONE` (a reviewable branch `g2/<run_id>/result` — never merged, never
checked out into your tree) or `HALT` (a failure report that preserves all work).
Human-readable reports land in `<run>/report.md`; stdout stays machine-readable JSON.

Useful flags: `--allow-dirty` (override the dirty-tree refusal), `--no-cache` (cold
run), `--skip-baseline`, `--checks <file>` (override ladder detection, lines of
`name|kind|cmd`), `--non-interactive` (default blocking questions, tagged
`[ASSUMPTION]`), `--answers-file <f>`.

## How it works

```
INTAKE → TRIAGE ┬─ fasttrack ─→ FASTTRACK ───────────────┐
                └─ full ─→ RECON → COVERAGE_CHECK ┬─ DISCOVERY ─┤
                           → CLARIFY → PROMPT_GEN → REVIEW      │
                           → EXECUTE ─→ INTEGRATE ─→ JUDGE ─→ APPLY
```

- **Three paths.** Triage routes trivial work to a single strong agent,
  undecomposable (discovery-shaped) work to a single strongest-model agent, and
  everything else through planning. All three end at the same gates — fasttrack
  shortens the road to the gates, it never routes around them.
- **Intelligence is spent before execution.** The strongest models run recon,
  prompting, and review. Execution runs cheap. If a prompt needs the executor to
  think, the prompt was underspecified.
- **Only the computer blocks.** LLM stages produce verdicts; the runner enforces
  them. Gates: dirty repo, coverage check, citation resolution, validate-plan,
  plan review, attack, citation pre-flight, check ladder, baseline delta,
  merge-tree, judge.
- **Every loop is capped** (escalate ×1, judge sendbacks ×2, kickbacks ×3,
  replans ×2/chain, …). Exhaustion produces a HALT report, not a silent loop.
- **Efficiency is structural:** artifact cache keyed by `baseline_sha`
  (`~/.gigga2/cache/`), test impact selection at chain gates, e2e only at
  integration, stable-prefix/varying-tail executor dispatch, and concurrency for
  everything independent by construction (baseline ladder ∥ triage ∥ recon,
  gap agents, body writers, plan review ∥ attack, chains within a wave).
- **Human and machine latency are measured separately** and reported at APPLY.

## Configuration

`~/.gigga2/config.json` (created on first run):

```json
{
  "models": { "strongest": null, "strong": null, "cheap": null },
  "runner": "opencode",
  "max_wall_seconds": 1800,
  "stall_seconds": 300
}
```

Tier values are opencode `provider/model` strings; `null` uses opencode's default
model. Env overrides: `GIGGA2_MODEL_STRONGEST`, `GIGGA2_MODEL_STRONG`,
`GIGGA2_MODEL_CHEAP`. The tier allocation and its reasoning are recorded in
[docs/model-allocation.md](docs/model-allocation.md).

## Evaluation

`eval/eval.py` runs task packs from `eval/tasks/*.json` against multiple arms
(`v2`, `cheap`, `strong`) and reports variance, machine vs human time, and cold vs
warm cache runtimes. See `eval/eval.py --help`-style docstring at the top of the file.

## Known limitations

Stated plainly, because the predecessor's central weakness stayed invisible for
months while its documentation described gates that did not exist:

- **Recon omission remains the deepest risk.** The keyword hit map catches
  *textual* evidence of relevant code. It cannot catch semantically relevant code
  sharing no vocabulary with the request. Nothing here reliably detects work that
  should have been planned and wasn't.
- **Attack measures clarity, not correctness.** A precise, wrong prompt passes it.
- **Triage decides before reading deeply.** That is the point, but fasttrack
  routing rests on a cheap model reading a structural summary. `ESCALATE` and the
  escalation rate are the mitigations; neither is a guarantee.
- **Cost is front-loaded and non-refundable.** A run rejected at plan review has
  already paid for recon and prompt generation.
- **Cold runs are slow.** The cache makes the median run fast; the first run
  against a new commit pays full price.
- **Wide parallelism is rare.** Most tasks yield one or two chains. The machinery
  is not built to only pay off at high concurrency.
- **Worse than a plain agent on mid-size tasks** — too big for fasttrack, too
  small to amortise planning.

## Master plan

The implementation specification this is built against is
[docs/gigga-v2-master-plan.md](docs/gigga-v2-master-plan.md)
(stage map §2, data model §3, stages §4, efficiency model §5, gates §6, loop caps
§7, model allocation §8, instrumentation §9, evaluation §10).
