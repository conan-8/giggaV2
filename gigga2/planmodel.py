"""Data model + computer-run validators (master plan §3).

Every stage assumes the exact field names below. Renaming after they land is
the staleness failure this architecture exists to prevent.

Prompt:    id, chain, seq, title, body, cites, cite_hash, touches, acceptance,
           assumes, records, gate
Decision:  id, question, assumed_value, actual_value, decided_by, affects
Chain:     id, title, prompts (ordered), depends_on
Plan:      chains, chain DAG, flat decision registry, interfaces, rationale
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

GATES = ("none", "chain", "integration")


@dataclass
class Prompt:
    id: str
    chain: str
    seq: int
    title: str
    body: str = ""
    cites: list[str] = field(default_factory=list)       # "path" or "path:start-end"
    cite_hash: dict[str, str] = field(default_factory=dict)  # anchor -> sha1 at plan time
    touches: list[str] = field(default_factory=list)     # declared write set
    acceptance: list[str] = field(default_factory=list)  # shell cmd or observable statement
    assumes: list[str] = field(default_factory=list)     # decision ids
    records: list[str] = field(default_factory=list)     # decision ids emitted on execution
    gate: str = "none"                                   # none | chain | integration


@dataclass
class Decision:
    id: str
    question: str
    assumed_value: str
    actual_value: str | None = None   # null until executed — do not collapse these
    decided_by: str = ""              # prompt id
    affects: list[str] = field(default_factory=list)  # prompt ids whose assumes contains id


@dataclass
class Chain:
    id: str
    title: str
    prompts: list[Prompt] = field(default_factory=list)  # ordered by seq
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    chains: list[Chain] = field(default_factory=list)
    decisions: dict[str, Decision] = field(default_factory=dict)  # flat registry
    interfaces: str = ""
    rationale: str = ""

    # ---- io -----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "chains": [
                {**asdict(c), "prompts": [asdict(p) for p in c.prompts]} for c in self.chains
            ],
            "decisions": {k: asdict(v) for k, v in self.decisions.items()},
            "interfaces": self.interfaces,
            "rationale": self.rationale,
        }

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        plan = Plan(interfaces=d.get("interfaces", ""), rationale=d.get("rationale", ""))
        for c in d.get("chains", []):
            chain = Chain(id=c["id"], title=c.get("title", ""),
                          depends_on=list(c.get("depends_on", [])))
            for p in c.get("prompts", []):
                p = dict(p)
                p.setdefault("chain", chain.id)
                chain.prompts.append(Prompt(**{k: v for k, v in p.items()
                                               if k in Prompt.__dataclass_fields__}))
            chain.prompts.sort(key=lambda p: p.seq)
            plan.chains.append(chain)
        for k, v in d.get("decisions", {}).items():
            v = {kk: vv for kk, vv in v.items() if kk in Decision.__dataclass_fields__}
            v.setdefault("id", k)
            plan.decisions[k] = Decision(**v)
        return plan

    def save(self, plan_dir: Path) -> None:
        plan_dir = Path(plan_dir)
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "plan.json").write_text(
            json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        if self.interfaces:
            (plan_dir / "interfaces.md").write_text(self.interfaces, encoding="utf-8")
        if self.rationale:
            (plan_dir / "rationale.md").write_text(self.rationale, encoding="utf-8")

    @staticmethod
    def load(plan_dir: Path) -> "Plan":
        return Plan.from_dict(
            json.loads((Path(plan_dir) / "plan.json").read_text(encoding="utf-8")))

    # ---- helpers --------------------------------------------------------

    def all_prompts(self) -> list[Prompt]:
        return [p for c in self.chains for p in c.prompts]

    def prompt(self, pid: str) -> Prompt | None:
        for p in self.all_prompts():
            if p.id == pid:
                return p
        return None

    def chain(self, cid: str) -> Chain | None:
        for c in self.chains:
            if c.id == cid:
                return c
        return None

    def waves(self) -> list[list[str]]:
        """Topological waves of chain ids. Raises ValueError naming the cycle."""
        deps = {c.id: [d for d in c.depends_on if self.chain(d)] for c in self.chains}
        done: set[str] = set()
        waves: list[list[str]] = []
        remaining = dict(deps)
        while remaining:
            ready = sorted(cid for cid, ds in remaining.items()
                           if all(d in done for d in ds))
            if not ready:
                cycle = _find_cycle(deps, done)
                raise ValueError(f"chain DAG has a cycle: {' -> '.join(cycle)}")
            waves.append(ready)
            done.update(ready)
            for cid in ready:
                del remaining[cid]
        return waves


def _find_cycle(deps: dict[str, list[str]], done: set[str]) -> list[str]:
    nodes = [n for n in deps if n not in done]
    visiting: list[str] = []
    visited: set[str] = set()

    def dfs(n: str) -> list[str] | None:
        if n in visiting:
            return visiting[visiting.index(n):] + [n]
        if n in visited:
            return None
        visiting.append(n)
        for d in deps.get(n, []):
            if d in done:
                continue
            r = dfs(d)
            if r:
                return r
        visiting.pop()
        visited.add(n)
        return None

    for n in nodes:
        r = dfs(n)
        if r:
            return r
    return nodes


# ---- validators (computer-run; master plan §3) -------------------------


def validate_plan(plan: Plan, repo: str | Path | None = None, baseline_sha: str | None = None,
                  waves: list[list[str]] | None = None) -> list[str]:
    """Return a list of validation failures (empty = valid). Never raises on bad plans."""
    from .repo import resolve_cite  # local import to keep module load light

    errors: list[str] = []

    # chain/seq unique and contiguous per chain
    seen_ids: set[str] = set()
    for c in plan.chains:
        seqs = sorted(p.seq for p in c.prompts)
        if seqs != list(range(1, len(c.prompts) + 1)):
            errors.append(f"chain {c.id}: seq not contiguous 1..N (got {seqs})")
        for p in c.prompts:
            if p.id in seen_ids:
                errors.append(f"duplicate prompt id {p.id}")
            seen_ids.add(p.id)
            if p.chain != c.id:
                errors.append(f"prompt {p.id}: chain field '{p.chain}' != owning chain '{c.id}'")
            if p.gate not in GATES:
                errors.append(f"prompt {p.id}: bad gate '{p.gate}'")

    # every assumes id exists in the registry
    for p in plan.all_prompts():
        for dec in p.assumes:
            if dec not in plan.decisions:
                errors.append(f"prompt {p.id}: assumes unknown decision '{dec}'")

    # every records id unique across the plan
    records_owner: dict[str, str] = {}
    for p in plan.all_prompts():
        for dec in p.records:
            if dec in records_owner:
                errors.append(
                    f"decision '{dec}' recorded by both {records_owner[dec]} and {p.id}")
            records_owner[dec] = p.id

    # decision registry consistency
    for did, d in plan.decisions.items():
        if d.decided_by and d.decided_by not in records_owner.values() and d.decided_by not in seen_ids:
            errors.append(f"decision {did}: decided_by unknown prompt '{d.decided_by}'")
        for aff in d.affects:
            p = plan.prompt(aff)
            if p is None:
                errors.append(f"decision {did}: affects unknown prompt '{aff}'")
            elif did not in p.assumes:
                errors.append(f"decision {did}: affects {aff} but {aff}.assumes lacks {did}")

    # chain DAG acyclic; name the cycle on failure
    try:
        computed = plan.waves()
        if waves is None:
            waves = computed
    except ValueError as e:
        errors.append(str(e))

    # unknown depends_on targets
    chain_ids = {c.id for c in plan.chains}
    for c in plan.chains:
        for d in c.depends_on:
            if d not in chain_ids:
                errors.append(f"chain {c.id}: depends_on unknown chain '{d}'")

    # every cites anchor resolves at baseline_sha
    if repo is not None and baseline_sha:
        for p in plan.all_prompts():
            for anchor in p.cites:
                if resolve_cite(repo, baseline_sha, anchor) is None:
                    errors.append(f"prompt {p.id}: cite does not resolve at baseline: {anchor}")

    # touches sets do not overlap across chains in the same wave
    if waves:
        for wave in waves:
            owner: dict[str, str] = {}
            for cid in wave:
                c = plan.chain(cid)
                if not c:
                    continue
                for p in c.prompts:
                    for t in p.touches:
                        if t in owner and owner[t] != cid:
                            errors.append(
                                f"touches overlap in wave: '{t}' in both {owner[t]} and {cid}")
                        owner[t] = cid
    return errors
