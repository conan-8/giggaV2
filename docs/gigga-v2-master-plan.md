# GIGGA v2 — Master Plan

An implementation specification. Read this end-to-end before writing code; the data model in
§3 is assumed by every stage after it, and the efficiency model in §5 changes how several
stages are built.

---

## 1. What this is

An agent pipeline for hard software engineering work in existing repositories: multi-module
features, refactors, schema changes, whole project phases. Input is a task and a repo. Output
is a reviewable git branch.

**Governing principle:** one expensive research pass that compiles a hard problem into
instructions simple enough to execute cheaply. Research once. Execute mechanically.

**Explicit non-goal:** speed at the cost of correctness. A slow correct result beats a fast
wrong one for the work this system targets.

**But every stage should be as fast as it can be without losing what makes it work.** Time spent
re-deriving something already known, waiting on work that could run concurrently, or running a
test suite that cannot possibly have been affected is pure loss. §5 specifies this; it is not
optional polish.

**Corollary that must survive implementation:** intelligence is spent *before* execution. The
strongest models run recon, prompting, and review. Execution runs cheap. If you find yourself
raising the executor's model tier or step budget to make something work, the prompt was
underspecified — fix it upstream.

**Three paths.** Triage routes trivial work to a single agent, undecomposable work to a single
agent, and everything else through planning. All three end at the same gates.

---

## 2. Stage map

| # | Stage | Runs on | Model | Can block? |
|---|---|---|---|---|
| 0 | INTAKE | computer | — | yes |
| 1 | TRIAGE | agent | cheap | routes |
| — | FASTTRACK | agent | strong | side path |
| 2 | RECON | agent | strongest | no |
| 3 | COVERAGE_CHECK | computer (+ agent on gap) | strong | yes |
| — | DISCOVERY | agent | strongest | side path |
| 4 | CLARIFY | agent + user | strong | yes |
| 5 | PROMPT_GEN | agent, then agents ∥ | strongest | no |
| 6 | REVIEW | 2 agents ∥ | strongest + strong | yes |
| 7 | EXECUTE | computer + agents ∥ | cheap | yes |
| 8 | INTEGRATE | computer (+ agent on conflict) | strong | yes |
| 9 | JUDGE | agent | strongest | yes |
| 10 | APPLY | computer | — | no |
| — | HALT | computer | — | terminal |

Terminal states: `DONE`, `HALT`.

Paths: **fasttrack** = 0,1,FASTTRACK,8,9,10. **discovery** = 0-3,DISCOVERY,8,9,10.
**full** = all.

Three consolidations from earlier drafts, each removing a stage boundary without removing work:
probe folded into INTAKE (both computer-only), classification folded into RECON (the agent that
read everything is the one qualified to judge whether the work is plannable, and asking it costs
one extra section in a file it is already writing), and plan review folded alongside attack into
a single concurrent REVIEW stage.

---

## 3. Data model

Define these first. Every later stage assumes the exact field names; renaming after they land is
the staleness failure this architecture exists to prevent.

### Prompt

```
id          stable, e.g. "p07"
chain       owning chain id
seq         1-based position within the chain
title       one line
body        the instruction text handed to an executor
cites       repo anchors, "path" or "path:start-end", resolvable at baseline_sha
cite_hash   content hash of each anchor at plan time — used by pre-flight (§4.7a)
touches     declared write set
acceptance  checkable criteria — shell command, or concrete observable statement
assumes     decision ids this prompt was written against
records     decision ids this prompt must emit when executed
gate        "none" | "chain" | "integration"
```

### Decision

```
id             "dec-004"
question       what was open
assumed_value  what the prompter assumed when writing downstream prompts
actual_value   what the executor actually chose; null until executed
decided_by     prompt id
affects        prompt ids whose `assumes` contains this id
```

`assumed_value` vs `actual_value` is the divergence machinery. Do not collapse them.

### Chain / Plan

```
Chain: id, title, prompts (ordered), depends_on (chain ids)
Plan:  chains, chain DAG, flat decision registry, interfaces, rationale
```

### Run state

Append-only `journal.jsonl` is authoritative; `state.json` is a derived cache. Deleting
`state.json` must reconstruct identical state by replay. Cache the sequence counter — do not
rescan the journal per append.

Bound at INTAKE: `repo_path`, `baseline_sha`, `baseline_branch`, `dirty`.

### Validators (computer-run)

- chain/seq unique and contiguous per chain
- every `assumes` id exists in the registry
- every `records` id unique across the plan
- chain DAG acyclic; name the cycle on failure
- every `cites` anchor resolves at `baseline_sha`
- `touches` sets do not overlap across chains in the same wave (overlap within a chain is fine)

---

## 4. Stages

### 0 · INTAKE

Computer only, no model. Four jobs, three of which run concurrently.

**Bind the repo.** Resolve and validate. Refuse a dirty working tree (`--allow-dirty`
overrides). Record `baseline_sha`. Everything downstream is anchored to it; a dirty tree makes
the run unsound.

**Check the artifact cache** (§5.1). If a prior run against this `baseline_sha` produced a
probe, findings, or baseline check results, reuse them and skip the corresponding work.

**Probe** — deterministic structural facts into `probe.md`:

- directory tree to depth 3, file counts, LOC by language
- dependency manifests: dependencies and scripts sections only
- detected framework, test runner, typechecker, linter, config paths
- `git log --oneline -30`, test file inventory
- import/require graph edges by grep (crude is fine — this is a map, not a compiler)
- **keyword hit map**: salient nouns from the request, grepped repo-wide, hit counts by directory

The hit map is load-bearing — it is the input to COVERAGE_CHECK and the only mechanical signal
for "there is relevant code here that nobody looked at." Cap output at ~8KB; truncate the tree,
never the manifests or the hit map.

**Baseline check ladder.** Detect it (package.json scripts, tsconfig, vitest/jest/playwright
configs, pyproject/pytest.ini, go.mod, Cargo.toml; `--checks <file>` overrides). Run it once
against baseline into `baseline-checks.json`, capturing individual test ids where the runner
emits machine-readable output. Empty ladder → write it and warn loudly; a run with no checks has
no objective gate.

**This runs concurrently with TRIAGE and RECON.** It is computer work that neither depends on.
Serialising it is free time lost.

### 1 · TRIAGE

Cheap agent. Reads the request and `probe.md` only — no deep repo reading, because the point is
to decide before paying for it. Dispatched as soon as the probe lands, without waiting for the
baseline suite.

```
{ "route": "fasttrack" | "full", "reasoning": "...", "signals": [...] }
```

**Fasttrack** when *all* hold: change plausibly confined to a small number of files; no
architectural, framework, schema, or public-API decision implied; no auth or permission logic;
request unambiguous on its face; keyword hit map not scattered across many modules.

**Full** otherwise, and **full on any uncertainty.** Over-planning a small task wastes tokens;
under-planning a large one wastes the run.

### FASTTRACK (side path)

One agent, strong model — it works unsupervised with no plan, the opposite of a job for a cheap
model. Full repo read, bash, edit in a dedicated worktree off `baseline_sha`. No prompt
generation, no chains, no review.

```
DONE: <one line summary>
ESCALATE: <reason this is bigger than triaged>
BLOCKED: <reason>
```

**`ESCALATE` matters more than `DONE`.** The agent that opens the files is the first to learn the
task is bigger than triaged. On escalate, discard the worktree and re-enter at RECON. Cap 1.

**Fasttrack skips planning. It does not skip verification.** Its output passes INTEGRATE, the
check ladder, the baseline delta, and JUDGE exactly as the full path does. A short road to the
gates is fine; a short road *around* them is how the predecessor shipped unverified work.

### 2 · RECON

One agent, one pass, strongest model. Full repo read, glob, grep, inspection-only bash (git
log/show/diff/ls-files, cat, ls, find, grep, wc, package manager info, test-runner list). Never
writes, installs, migrates, or executes project code — running the suite is the runner's job.

Reads `probe.md` first and treats it as ground truth. It does not re-derive what the probe
already established; its job is interpretation, not inventory.

Emits `recon/findings.md`:

- **Layout** — where source, tests, config, generated code live
- **Stack** — language, framework and version, package manager, build tool
- **Test setup** — runner, config, invocation, current pass state (read `baseline-checks.json`
  when available; do not run anything)
- **Conventions** — module boundaries, naming, error handling, patterns relevant to the request
- **Touch set** — files this request will likely modify, their dependents, blast radius per file
- **Existing interfaces** — real signatures of anything the work will call or extend
- **Risks** — pre-existing failures, dead code, sharp edges
- **Coverage statement** — every directory in the probe's keyword hit map, marked *examined* or
  *excluded*, with a reason for every exclusion
- **Classification** — `execution` or `discovery`, with confidence and signals (see below)

**Stopping condition — this is the efficiency control for the longest agent stage.** Recon is
done when it can name the touch set with citations that resolve, state the blast radius of each
file, and give the existing interfaces the work will call. If it cannot do that, it is not
finished. If it can, further reading is waste. Put this in the agent prompt verbatim; it makes
recon depth scale with task complexity automatically, with no extra stage or configuration.

**Rules:** every claim cites a path, and a line range where useful. An uncitable finding is a
guess and does not belong in findings.md. No brevity target — output is consumed by models with
no shared context.

**Classification criteria**, stated in the prompt body:

*Execution-shaped* — the change set is knowable by reading code, success is definable in advance,
you can name the files before starting.

*Discovery-shaped* — requires observing behaviour you cannot predict, fix location unknown,
progress depends on what earlier steps reveal.

**Low confidence routes to DISCOVERY.** A capable agent with a long leash degrades gracefully on
plannable work; a prompt chain built on unknowns fails confidently and expensively. Because
recon has just done the work, it has a mild incentive to declare the task plannable — counter it
explicitly: *"You have just spent significant effort reading this codebase. That is not evidence
the task is plannable. Judge the task, not your investment in it."*

### 3 · COVERAGE_CHECK

**This exists because recon is otherwise ungated and everything downstream inherits from it.**

Computer step, no model:

- every directory with significant keyword hits in `probe.md` appears in the coverage statement
- every exclusion carries a stated reason
- every citation in `findings.md` resolves

On an unaccounted directory, dispatch a **gap agent** (strong model) scoped to that directory
alone: *"the request mentions X, this directory has N matches for X, recon did not examine it —
does it contain relevant code?"* Its finding is appended, or the exclusion is confirmed. Multiple
gaps dispatch concurrently — they are independent by construction.

Unresolvable citations, or a gap agent reporting materially relevant code missed twice → HALT.

The coverage statement travels forward: REVIEW and JUDGE both read it. Every other gate validates
that what is *present* is correct. This is the only artifact that makes an absence visible.

### DISCOVERY (side path)

Single agent, strongest model. Full read, bash including the test suite, edit in a dedicated
worktree. Long step budget. No decomposition.

- `FOUND: <cause>` → record, re-classify the now-narrowed task
- `FIXED: <summary>` → proceed to INTEGRATE
- `BLOCKED: <reason>` → HALT

Once a cause is found the remaining fix is usually execution-shaped, so the re-entry path matters.

### 4 · CLARIFY

Reads request + findings. Emits `questions.md`, then `answers.md` after the user responds.

**Blocking by definition** when a wrong answer forces rework across more than one prompt. Always:
architecture or runtime target; framework or major library; data model or schema; public API or
exported interface; auth or permission logic; anything altering behaviour users depend on.

**No cap on blocking questions.** In the prompt verbatim:

> Asking three good questions is cheaper than one wrong architecture. If you find yourself
> reasoning "this is the fundamental decision, but I'll default it" — that is the definition of
> blocking. Stop and ask.

Asking and not asking must be equally cheap for the agent; any shortcut available only on the
zero-questions path will be taken. Batch all blocking questions into **one** interaction — each
additional round trip costs human latency, which dwarfs everything else in this pipeline.

Non-blocking items get `[ASSUMPTION]`-tagged defaults, surfaced again at APPLY. A question the
repo already answers is a recon failure, not a user question.

### 5 · PROMPT_GEN

**A research agent that emits prompts as its output format — not a splitter.** Splitting work
into seventeen pieces takes ninety seconds; knowing what the seventeen should be takes the whole
investigation. If this stage becomes a decomposition pass, prompts come out shallow, thinking
falls back onto executors, and the architecture collapses. Name that failure mode in the prompt.

Two phases. The split is what makes the longest machine stage parallel.

**5a · Skeleton** — one strongest-model agent. Emits chains, the DAG, the decision registry with
`assumed_value`s, the interface contract, and `rationale.md`. No prompt bodies. This is the part
that requires whole-task context and cannot be parallelised.

**5b · Bodies** — one agent per chain, concurrent, strongest model. Each receives findings,
answers, the skeleton, the full interface contract, and its own chain's prompt stubs. Writes
`body`, `cites`, `cite_hash`, `touches`, `acceptance` for its chain only. Body-writing is where
the tokens are and it is independent once the skeleton is fixed.

Rules for both phases:

- Every body must be executable by a cheap model with no repo knowledge beyond what the prompt
  provides. If it needs to think, you have not finished.
- Quote real code. Real paths, signatures, line ranges, verified against the repo.
- State the defect before the fix — current code, then what it becomes.
- Concrete acceptance per prompt. A shell command where possible. "Works correctly" is not
  acceptance.
- Chains are sequential and dependent. That is normal. Express real ordering; do not flatten
  dependency to manufacture parallelism.
- **Prompt granularity: a prompt is the largest unit of work that still requires no thinking.**
  Runtime scales with prompt count times a fixed per-prompt overhead — context load, dispatch,
  pre-flight, reply parse. Twenty-five prompts where ten would do costs 2.5× for nothing. Target
  5–15 for a typical task; the runner warns above 20.
- Declare decisions. Any choice a later prompt assumes goes in `records` with `assumed_value` and
  in every dependent prompt's `assumes`. **A conditional fallback ("if X isn't possible, do Y")
  IS a decision and must be declared.**
- Full prose. No compression.

The skeleton agent runs `validate-plan` after 5b completes and fixes failures before reporting.

### 6 · REVIEW

Two agents, **run concurrently**, both verdicts required. They ask different questions of
different inputs and neither depends on the other.

**6a · Plan review** — strongest model, small input: verbatim request, `rationale.md`, chain
titles, decision registry, coverage statement. Not the prompt bodies. One question: *does this
plan, if executed perfectly, deliver what was asked?*

```
PASS | REJECT-PLAN
[chain_id | decision_id] request asked X, plan does Y
```

Checks architecture, runtime target, framework, data model, every `[ASSUMPTION]`, and **anything
the request implies that the coverage statement shows was excluded**.

**6b · Attack** — strong model, reads every prompt **cold**. Repo and `plan/` access only,
deliberately denied `findings.md` and `rationale.md`: an attacker who can see the prompter's
reasoning fills gaps from it, which defeats the point. **Prompts are batched across concurrent
attackers in groups of five** — each is read independently by construction, so this is near-linear
speedup on a stage that otherwise scales with prompt count.

Per prompt: can I name the exact file and location from this alone? Do cited paths and signatures
resolve? Is acceptance checkable or a vibe? Unresolved reference — "the appropriate file", "as
needed", "update accordingly"? Knowledge from a sibling not declared in `assumes`? Conditional
fallback with no declared decision? One coherent unit, or three tasks in a trenchcoat?

```
PASS | KICKBACK
[prompt_id] <what a cold executor would have to guess>
```

Computer pre-check before both: `validate-plan`.

Routing: `REJECT-PLAN` → CLARIFY, surfacing defects to the user first (cap 2, shared with judge).
`KICKBACK` → PROMPT_GEN 5b for the affected chains only (cap 3).

**Known limit, do not paper over it:** attack measures clarity, not correctness. A precise,
well-cited, confidently wrong prompt passes it. 6a and JUDGE catch wrongness; 6b catches
vagueness. Different questions — which is exactly why they run as separate agents.

### 7 · EXECUTE

One worktree per **chain**, branched from `baseline_sha`:

```
git -C <repo> worktree add <run>/worktrees/<chain> -b g2/<run_id>/<chain> <baseline_sha>
```

Per-chain, not per-prompt — that is what lets prompt 3 see prompt 1's real edits. Never checkout,
never touch the user's working tree.

Waves: topologically sort the chain DAG; run each wave's chains concurrently.

Per prompt, in order:

**a. Citation pre-flight (computer).** Re-hash every `cites` anchor in the worktree's current
state and compare to `cite_hash`. On mismatch, do not dispatch — mark the chain remainder STALE
and replan (step e). This closes the "weakest agent meets reality" gap: without it, a cheap
executor holds a prompt describing code that no longer exists and either fails usefully or
improvises catastrophically. Deterministic detection beats hoping the model notices, and hashing
is microseconds.

**b. Inject** the body plus the **actual** values of every decision in `assumes` — not the assumed
values. Assemble the dispatch as a **stable prefix plus a varying tail** (§5.3) so prompt caching
holds across a chain.

**c. Dispatch.** Cheap fast model, modest step budget, read allow, edit within its worktree, bash
allow.

```
DONE
records:
  <decision-id>: <actual_value>
-- or --
BLOCKED: <reason>
```

The executor **never reports check results**. The runner runs checks. In its prompt: *"If the code
does not match what this prompt describes, reply BLOCKED. Never improvise a fix."*

**d. Record** actual values into `decisions.json`.

**e. Divergence check.** For every decision where `actual_value != assumed_value`, find downstream
prompts whose `assumes` contains it. If any exist, mark the chain remainder STALE and route back
to PROMPT_GEN 5b for that chain only. The prompter rewrites **only the remainder**; completed
prompts and their edits stand. **Replanned prompts re-enter attack (6b) before executing** — they
are the ones most likely to be vague. Cap replans at 2 per chain.

**f. Gate.** If `prompt.gate == "chain"`, run the ladder in that worktree under the cost controls
in §5.2.

Bounds per chain: `max_wall_seconds` (default 1800), heartbeat stall threshold (default 300s).
Over-budget or stalled → record failure, move on, do not block the wave.

**Attempt tracking is per chain, never global.** A shared counter halts a six-chain run when four
different chains each fail once.

### 8 · INTEGRATE

Branch `g2/<run_id>/result` off `baseline_sha`. Merge chain branches in dependency order, using
`git merge-tree` to detect conflicts before each merge.

- Clean merge → proceed, **zero AI involvement**
- Real conflict → dispatch a merge agent with the conflicting hunks only, both sides, the relevant
  prompt bodies, and the decision registry

**Single-chain runs skip conflict detection entirely** — there is nothing to conflict with. This
covers fasttrack, discovery, and a large share of full-path runs.

Merge agent rule: *"Two chains editing different hunks of one file is normal and git already
merged it. You are seeing a real semantic conflict. If the two sides are incompatible by design,
that is a planning defect — report it rather than inventing a compromise."*

Support re-integrating a single replanned chain without reprocessing the rest.

Then the full ladder and the baseline delta:

| class | meaning | action |
|---|---|---|
| regression | passed at baseline, fails now | hard failure |
| new-failing | new test, fails | hard failure |
| fix | failed at baseline, passes now | informational |
| new-passing | new test, passes | informational |
| still-failing | failed at baseline, fails now | **ignore** |

`still-failing` matters: a pre-existing broken test must never block a run or trigger rebuild
loops. Attribute regressions by intersecting the failing test's source path with each chain's
diff; unattributable → integration.

### 9 · JUDGE

Strongest model. Read allow. Bash allow, constrained to read-only verification — it must be able
to run the suite to check a claim.

Input: verbatim request, `answers.md`, `git diff baseline..result`, check and delta results, the
coverage statement, repo path for reading unchanged context. **Not the whole tree** — a judge
staring at a codebase cannot tell what changed.

```
ACCEPT | REJECT | REJECT-PLAN
REJECT      [prompt_id] <implementation does not satisfy the plan>
REJECT-PLAN [chain_id | decision_id] request asked X, plan did Y
```

Rules: the original request outranks the plan — a faithful implementation of a drifted plan is
REJECT-PLAN, not ACCEPT. Passing checks prove the code runs, not that it does what was asked. Read
the coverage statement; if the request implies work in an excluded directory, that is REJECT-PLAN
even though the diff is internally consistent.

**Every judge sendback is capped at 2.** `REJECT` → re-exec named prompts, twice at most.
`REJECT-PLAN` → CLARIFY, twice at most, sharing that budget with plan review. On fasttrack,
`REJECT` returns to the fasttrack agent twice, then escalates to the full path — a route change,
not a failure.

Two is deliberate. One sendback catches a defect the executor can fix. A second catches one it
needed a hint to fix. A third means judge and executor disagree about something neither can
resolve by trying again, and further rounds burn budget converging on nothing.

On the second failed sendback: HALT with a failure report.

### 10 · APPLY

Never checkout, never merge to main, never touch the working tree. Leave a reviewable branch and
report: branch name and diffstat; path taken; check results; delta summary; **`[ASSUMPTION]`
defaults applied without confirmation**; **coverage statement**; per-stage wall clock and tokens,
with human wait time reported separately (§5.5).

The assumption list and coverage statement are the user's last chance to catch what every gate
missed.

`rollback` removes every branch and worktree the run created, leaving `git status` clean.

### HALT — failure report

Reachable from every exhausted loop, and the only non-`DONE` terminal state. A halt is a result,
not a crash: the run must explain itself well enough to act on without reading the journal.

**Preserve everything.** Do not roll back on halt. Branch, worktrees, and partial work stay on
disk. `rollback` remains manual.

Write and print `report.md`:

```
FAILED — <one line: which gate, which stage>

What you asked for      <verbatim request>

What got built          branch g2/<run_id>/result · <diffstat>
                        path taken: fasttrack | discovery | full
                        <what actually landed, one paragraph>

Where it stuck          <persisting defect, quoted from the final verdict,
                         with cited file and hunk>

What was tried          attempt 1 — <what changed> → <why rejected>
                        attempt 2 — <what changed> → <why rejected again>

Why it could not resolve
                        <code wrong | plan wrong | request ambiguous>

Where to look           <files to open first>

What would unblock it   <concrete options — a decision only the user can make,
                         a question the repo cannot answer, a constraint to relax>
```

**What was tried** earns its place: "it failed twice" teaches nothing, but "both attempts
converged on the same wrong interface" tells the user exactly which assumption to fix. Both
attempts must be described distinctly — if attempt 2 is indistinguishable from attempt 1, that is
itself the finding and the report says so.

**Why it could not resolve** must name a category. *Code wrong* → re-run with a hint. *Plan wrong*
→ answer a clarify question and re-run. *Request ambiguous* → rewrite the task. Different actions;
guessing between them is the user's most expensive mistake.

Halts before JUDGE fill the same structure from the stage that halted. Sections with nothing to
report say so explicitly — an absent section reads as an oversight.

---

## 5. Efficiency model

Not optimisation to do later. Several of these change how a stage is built.

### 5.1 Artifact cache, keyed by `baseline_sha`

The largest single win, because real usage is several tasks against the same commit and every
run currently pays full price for facts that have not changed.

Cache `probe.md`, `findings.md`, and `baseline-checks.json` under
`~/.gigga2/cache/<repo-id>/<baseline_sha>/`. On INTAKE, reuse any that hit. Invalidate on any new
commit — never on a timer, never partially. `--no-cache` forces a cold run.

Findings are request-specific in their touch set but not in their layout, stack, conventions, or
interfaces. Split `findings.md` accordingly: a **repo section** (cacheable) and a **task section**
(regenerated per request). A second task against the same commit skips most of the longest agent
stage in the pipeline.

### 5.2 Gate cost control

The suite is the most-repeated expensive operation in a run. Three rules:

- **Never run e2e on a chain gate.** It belongs at integration only.
- **Test impact selection.** Run only tests whose imports transitively reach the chain's changed
  files, derived from the probe's import graph. Fall back to the full suite when the graph is
  unavailable or the change touches config, and say which mode ran.
- **Single-chain runs skip the chain gate entirely** and run only the integration gate. Otherwise
  the same suite runs twice against the same code.

### 5.3 Context reuse

Executors receive a **stable prefix** (interface contract, conventions excerpt, chain summary)
plus a **varying tail** (this prompt, its injected decisions). Identical prefix across a chain
means prompt caching holds, and cache-read tokens are an order of magnitude cheaper than fresh
input. Never interleave varying content into the prefix.

Recon reads `probe.md` rather than re-deriving structure. Judge reads the diff rather than the
tree. The rule generalises: no stage re-derives what an earlier artifact already established.

### 5.4 Concurrency map

Everything here is independent by construction, not by optimism:

| Concurrent | Why safe |
|---|---|
| baseline ladder ∥ TRIAGE ∥ RECON | computer work; neither agent depends on it |
| gap agents (COVERAGE_CHECK) | each scoped to one directory |
| chain body writers (5b) | independent once the skeleton is fixed |
| plan review ∥ attack (6a ∥ 6b) | different inputs, different questions |
| attack batches of 5 | each prompt read cold and alone |
| chains within a wave | disjoint `touches`, separate worktrees |

Serial by necessity: skeleton before bodies, prompts within a chain, waves.

### 5.5 Measure human and machine latency separately

CLARIFY blocks on a person. If that is twenty minutes it will dominate every timing chart and
send you optimising the wrong stage. Log `wall_clock_machine` and `wall_clock_human` as distinct
numbers, and report both at APPLY.

### 5.6 What not to cut

Do not shorten recon below its stopping condition, skip a gate because a run "looks clean", raise
the executor model tier to compensate for thin prompts, or drop the coverage statement because it
is rarely non-empty. Each is a real cost paid for a real property. **Efficiency here means
removing waste, never removing verification** — the check ladder, baseline delta, and judge run on
all three paths without exception.

---

## 6. Gate summary

**Only the computer blocks.** LLM stages produce verdicts; the runner enforces them. No agent ever
passes or fails its own work, and no agent's self-reported exit code is trusted anywhere.

| Gate | Type | Blocks on |
|---|---|---|
| dirty repo | computer | uncommitted changes |
| coverage check | computer | keyword-hit directory unaccounted for |
| citation resolution | computer | unresolvable anchor |
| validate-plan | computer | schema, DAG, dangling ids |
| plan review | agent verdict | plan ≠ request |
| attack | agent verdict | executable without guessing |
| citation pre-flight | computer | cited code changed since planning |
| check ladder | computer | typecheck, lint, unit, e2e exit codes |
| baseline delta | computer | regression or new-failing |
| merge-tree | computer | real conflict |
| judge | agent verdict | diff ≠ request |

**All three paths pass the last five.** Fasttrack shortens the road to the gates; it does not
route around them.

---

## 7. Loop caps

| Loop | From → To | Cap | On exhaustion |
|---|---|---|---|
| coverage gap | COVERAGE_CHECK → gap agent | 2 | HALT + report |
| fasttrack escalate | FASTTRACK → RECON | 1 | stays on full path |
| fasttrack reject | JUDGE → FASTTRACK | 2 | escalate to full path |
| plan reject | REVIEW 6a → CLARIFY | 2 (shared) | HALT + report |
| kickback | REVIEW 6b → PROMPT_GEN 5b | 3 | HALT + report |
| replan | EXECUTE → 5b → 6b | 2 per chain | HALT chain + report |
| re-exec | JUDGE → EXECUTE | 2 per prompt | HALT + report |
| judge plan reject | JUDGE → CLARIFY | 2 (shared) | HALT + report |

Every loop is capped. Every judge-originated loop caps at 2. Every exhaustion produces the failure
report, preserves the work, and surfaces to the user rather than looping or silently degrading.

---

## 8. Model allocation

| Tier | Stages |
|---|---|
| strongest | RECON, PROMPT_GEN 5a+5b, plan review, JUDGE, DISCOVERY |
| strong | FASTTRACK, gap agent, attack, merge agent |
| cheap | TRIAGE, EXECUTE |

Attack gets a strong model despite being small: it protects the entire execution budget, and a
lenient attacker is worse than none. Fasttrack gets a strong model because it works unsupervised
with no plan.

Record the allocation and its reasoning in a file — uniform allocation is the state everything
drifts back toward.

No agent may carry a compression or brevity directive on **artifacts or inter-agent replies**.
Concise user-facing status lines are fine; compressed clause text, prompt bodies, interface
descriptions, and defect reports are not — they are read by models with no shared context.

---

## 9. Instrumentation

- **`fork_rate`** — share classified discovery. If high, the pipeline is not earning its
  complexity.
- **`fasttrack_rate`** and **`fasttrack_escalation_rate`** — the second is the honest measure of
  triage quality. High escalation → triage too permissive. Near-zero fasttrack → too conservative,
  and ordinary tasks are paying for planning they do not need.
- **`cache_hit_rate`** — how often probe/findings/baseline were reused. Directly predicts median
  runtime.
- **`prompt_count`** and per-prompt overhead — the multiplier in §5's granularity rule.
- `coverage_gaps_found` — if never non-zero, either recon is excellent or keyword extraction is too
  narrow to catch anything. Check which.
- `kickback_rounds`, `plan_review_rejects`, `replan_count`, `judge_verdict`
- `halt_reason` and `stuck_category` — aggregated, this tells you which stage is actually failing
  you. Mostly *plan wrong* → the prompter. Mostly *request ambiguous* → clarify is too permissive.
- `wall_clock_machine` and `wall_clock_human`, separately, per stage
- tokens per stage: input, output, reasoning, cache read
- `baseline_delta` counts; `plan_drift` scored against ground truth in evaluation

Flag automatically: any stage over 2× the median for its type; any chain replanned more than once;
any prompt executed more than twice; **any gate that passed with zero checks configured**.

Provide a `timeline` command rendering the journal as one line per event with elapsed offsets.

---

## 10. Evaluation

Task set of 8–12 real tasks from repo history: 3 bug fixes with known-correct patches, 2 features
spanning 3+ modules, 2 refactors, 1 schema change with migration, 1 full phase, 2 deliberately
underspecified, 1 discovery-shaped, 2 genuinely trivial (to test triage).

Arms: **(A)** v2, **(B)** plain plan+build cheap, **(C) plain plan+build strongest**. Run arm C
first — if v2 cannot beat a strong model with a simple loop, the gains available are in model
allocation, not architecture.

Three repeats per task per arm. Report variance; do not average it away. Report machine time and
human time separately, and report cold-cache and warm-cache runtimes separately — the median real
run is warm.

---

## 11. Known limitations

State these in the README. The predecessor's central weakness stayed invisible for months because
its documentation described gates that did not exist.

- **Recon omission remains the deepest risk.** The keyword hit map catches *textual* evidence of
  relevant code. It cannot catch semantically relevant code sharing no vocabulary with the request.
  Nothing here reliably detects work that should have been planned and wasn't.
- **Attack measures clarity, not correctness.** A precise, wrong prompt passes it.
- **Triage decides before reading deeply.** That is the point, but fasttrack routing rests on a
  cheap model reading a structural summary. `ESCALATE` and the escalation rate are the mitigations;
  neither is a guarantee.
- **Cost is front-loaded and non-refundable.** A run rejected at plan review has already paid for
  recon and prompt generation.
- **Cold runs are slow.** The cache makes the median run fast; the first run against a new commit
  pays full price.
- **Wide parallelism is rare.** Most tasks yield one or two chains. Do not build machinery that
  only pays off at high concurrency.
- **Worse than a plain agent on mid-size tasks** — too big for fasttrack, too small to amortise
  planning. Say so.

---

## 12. Build order

```
Foundations   run state + repo binding → probe → data model + validators
              → worktree lifecycle → check ladder + baseline delta + cache
Fast path     triage → fasttrack → integrate → judge → apply
              (a complete working system, end to end, before planning exists)
Planning      recon + classification → coverage check → discovery
              → clarify → prompt gen 5a/5b → review 6a/6b
Execution     chain executor + decisions + divergence + pre-flight
              → wave scheduler + bounds → merge conflicts
Operations    model allocation → instrumentation → eval harness
```

Building the fast path second is deliberate: it exercises worktrees, the check ladder, the
baseline delta, integrate, judge, and apply end-to-end on a trivial task, before any planning
machinery exists to obscure a failure in them.

**Checkpoints — stop and evaluate, do not build straight through.**

*After foundations:* verify the check ladder, baseline delta, and cache on your actual repo.
Nothing above them is falsifiable until they work.

*After the fast path:* run a real trivial task end to end. You should get a branch, a diffstat, and
a clean delta. If not, fix it before building planning.

*After PROMPT_GEN, before building any execution:* run the prompter on a task whose answer you
already know and read twenty generated prompt bodies. Do they quote real code and name real files,
or is it plausible-sounding scaffolding? **This is the entire bet.** If the prompter finishes in
two minutes it did not research, and there is nothing to compile down. No downstream stage can
compensate.

*After APPLY:* run the eval harness against arm C, warm and cold.
