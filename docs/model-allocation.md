# Model allocation (master plan §8)

Uniform allocation is the state everything drifts back toward. This file records
the allocation and its reasoning so the drift is visible.

| Tier      | Stages | Why |
|---|---|---|
| strongest | RECON, PROMPT_GEN 5a+5b, plan review, JUDGE, DISCOVERY | These stages decide *what* gets built. Every downstream token is spent against their output; a wrong recon or a shallow prompt wastes the entire execution budget. |
| strong    | FASTTRACK, gap agent, attack, merge agent | Fasttrack works unsupervised with no plan — the opposite of a job for a cheap model. Attack protects the entire execution budget; a lenient attacker is worse than none. Gap agents and merge agents make judgement calls the gates inherit. |
| cheap     | TRIAGE, EXECUTE | Triage reads a probe and picks a route. Executors run prompts written so they need no repo knowledge beyond what the prompt provides — if execution needs a stronger model, the prompt was underspecified; fix it upstream, never here. |

## The corollary that must survive implementation

Intelligence is spent *before* execution. If you find yourself raising the
executor's model tier or step budget to make something work, the prompt was
underspecified — the fix belongs in PROMPT_GEN, not in this table.

## Where the mapping lives

`~/.gigga2/config.json` → `models: { strongest, strong, cheap }`, opencode
`provider/model` strings, `null` = opencode default. Env overrides:
`GIGGA2_MODEL_STRONGEST` / `GIGGA2_MODEL_STRONG` / `GIGGA2_MODEL_CHEAP`.

## Brevity rule

No agent may carry a compression or brevity directive on artifacts or inter-agent
replies. Concise user-facing status lines are fine; compressed clause text, prompt
bodies, interface descriptions, and defect reports are not — they are read by
models with no shared context.
