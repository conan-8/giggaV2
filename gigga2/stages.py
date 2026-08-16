"""The pipeline (master plan §2, §4).

Stage map:
    0 INTAKE (computer) · 1 TRIAGE (cheap) · FASTTRACK (strong, side) ·
    2 RECON (strongest) · 3 COVERAGE_CHECK (computer + gap agents) ·
    DISCOVERY (strongest, side) · 4 CLARIFY (strong + user) ·
    5 PROMPT_GEN 5a skeleton / 5b bodies∥ (strongest) ·
    6 REVIEW 6a plan ∥ 6b attack (strongest + strong) ·
    7 EXECUTE (computer + cheap agents∥) · 8 INTEGRATE (computer + merge agent) ·
    9 JUDGE (strongest) · 10 APPLY (computer) · HALT (computer, terminal)

Paths: fasttrack = 0,1,FASTTRACK,8,9,10 · discovery = 0-3,DISCOVERY,8,9,10 · full = all.
All three paths pass the check ladder, baseline delta, and judge without exception (§5.6).
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import prompts as P
from .agents import dispatch, dispatch_parallel, parse_records, parse_verdict
from .cache import ArtifactCache, merge_findings, split_findings
from .checks import compute_delta, detect_ladder, run_ladder, select_impacted_tests
from .journal import RunState
from .planmodel import Plan, validate_plan
from .probe import generate_probe
from .report import render_apply, render_halt
from . import repo as R
from .repo import GitError
from . import worktrees as W


class Halt(Exception):
    """Terminal failure. Carries the fields for the HALT report (§4.HALT)."""

    def __init__(self, gate: str, stage: str, stuck: str, category: str,
                 where_to_look: list[str] | None = None, unblock: list[str] | None = None):
        super().__init__(f"{stage}/{gate}: {stuck}")
        self.gate, self.stage, self.stuck = gate, stage, stuck
        self.category = category
        self.where_to_look = where_to_look or []
        self.unblock = unblock or []


class Pipeline:
    def __init__(self, state: RunState, cfg: dict, *, repo_path: str | None = None,
                 request: str | None = None, allow_dirty: bool = False,
                 no_cache: bool = False, skip_baseline: bool = False,
                 checks_file: str | None = None, non_interactive: bool = False,
                 answers_file: str | None = None, force_route: str | None = None):
        self.state = state
        self.cfg = cfg
        self.non_interactive = non_interactive
        self.answers_file = answers_file
        self._opts = {"allow_dirty": allow_dirty, "no_cache": no_cache,
                      "skip_baseline": skip_baseline, "checks_file": checks_file,
                      "force_route": force_route}
        self._repo_path = repo_path
        self._request = request
        self.cache: ArtifactCache | None = None
        self.ladder: list = []

    # ------------------------------------------------------------------
    # properties

    @property
    def repo(self) -> str:
        return self.state.state["repo_path"]

    @property
    def sha(self) -> str:
        return self.state.state["baseline_sha"]

    @property
    def request(self) -> str:
        return self.state.state.get("request") or self._request or ""

    @property
    def run_dir(self) -> Path:
        return self.state.run_dir

    def flag(self, name: str) -> bool:
        return bool(self.state.state.get("flags", {}).get(name))

    def set_flag(self, name: str, value: bool = True) -> None:
        flags = dict(self.state.state.get("flags", {}))
        flags[name] = value
        self.state.set(flags=flags)

    def read_artifact(self, rel: str) -> str:
        p = self.run_dir / rel
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ------------------------------------------------------------------
    # entry point

    def run(self) -> dict:
        s = self.state
        try:
            if s.state["phase"] == "INTAKE" and not self.flag("intake_done"):
                self.stage_intake()
            if s.state["phase"] == "TRIAGE":
                self.stage_triage()
            if s.state.get("path") == "fasttrack" and not self.flag("fasttrack_done"):
                self.stage_fasttrack()
            if s.state["phase"] == "RECON" and not self.flag("recon_done"):
                self.stage_recon()
            if s.state["phase"] == "COVERAGE_CHECK" and not self.flag("coverage_done"):
                self.stage_coverage_check()
            if s.state.get("path") == "discovery" and not self.flag("discovery_done"):
                self.stage_discovery()
            if s.state["phase"] == "CLARIFY" and not self.flag("clarify_done"):
                self.stage_clarify()
            if s.state["phase"] == "PROMPT_GEN" and not self.flag("promptgen_done"):
                self.stage_prompt_gen()
            if s.state["phase"] == "REVIEW" and not self.flag("review_done"):
                self.stage_review()
            if s.state["phase"] == "EXECUTE" and not self.flag("execute_done"):
                self.stage_execute()
            if s.state["phase"] == "INTEGRATE" and not self.flag("integrate_done"):
                self.stage_integrate()
            if s.state["phase"] == "JUDGE" and not self.flag("judge_done"):
                self.stage_judge()
            if s.state["phase"] == "APPLY" and not s.state.get("terminal"):
                return self.stage_apply()
            return self._summary(s.state.get("terminal") or "DONE")
        except Halt as h:
            return self.stage_halt(h)
        except GitError as e:
            return self.stage_halt(Halt("git", s.state["phase"], str(e), "code wrong"))
        except Exception as e:  # noqa: BLE001 — a halt is a result, not a crash
            import traceback
            return self.stage_halt(Halt(
                "internal error", s.state["phase"],
                f"{type(e).__name__}: {e}", "code wrong",
                where_to_look=[str(self.run_dir / "journal.jsonl")],
                unblock=[traceback.format_exc()[-800:]]))

    def _summary(self, terminal: str) -> dict:
        s = self.state.state
        return {
            "ok": terminal == "DONE",
            "phase": terminal,
            "run_id": s.get("run_id"),
            "state_dir": str(self.run_dir),
            "path": s.get("path"),
            "branch": f"g2/{s.get('run_id')}/result" if terminal == "DONE" else None,
            "warning": self._warnings(),
        }

    def _warnings(self) -> str | None:
        warns = []
        if self.state.state.get("checks_empty"):
            warns.append("check ladder is EMPTY — this run had no objective gate")
        return "; ".join(warns) or None

    # ------------------------------------------------------------------
    # 0 · INTAKE

    def stage_intake(self) -> None:
        s = self.state
        t0 = time.time()
        if not s.state.get("baseline_sha"):
            binding = R.bind_repo(self._repo_path, allow_dirty=self._opts["allow_dirty"])
            if self._request:
                binding["request"] = self._request
            s.set(phase="INTAKE", **binding)
        s.emit("stage_started", {"stage": "INTAKE"})
        self.cache = ArtifactCache(s.state["repo_path"], s.state["baseline_sha"],
                                   enabled=not self._opts["no_cache"])
        s.emit("cache_status", self.cache.status())

        (self.run_dir / "recon").mkdir(exist_ok=True)
        (self.run_dir / "plan").mkdir(exist_ok=True)

        # Probe and baseline ladder are computer work that neither TRIAGE nor RECON
        # depends on — run them concurrently (§4.0, §5.4).
        def do_probe() -> None:
            if self.cache.has("probe.md") and self.cache.has("probe.json"):
                (self.run_dir / "probe.md").write_text(
                    self.cache.read("probe.md"), encoding="utf-8")
                (self.run_dir / "probe.json").write_text(
                    self.cache.read("probe.json"), encoding="utf-8")
                s.emit("cache_hit", {"artifact": "probe"})
                return
            generate_probe(Path(self.repo), self.request,
                           self.run_dir / "probe.md", self.run_dir / "probe.json")
            self.cache.write("probe.md", (self.run_dir / "probe.md").read_text(encoding="utf-8"))
            self.cache.write("probe.json", (self.run_dir / "probe.json").read_text(encoding="utf-8"))

        def do_baseline() -> None:
            override = Path(self._opts["checks_file"]) if self._opts["checks_file"] else None
            self.ladder = detect_ladder(Path(self.repo), override)
            if self._opts["skip_baseline"]:
                data = {"ok": True, "skipped": True, "checks": [], "tests": {}, "empty": not self.ladder}
                (self.run_dir / "baseline-checks.json").write_text(json.dumps(data, indent=2))
                return
            if self.cache.has("baseline-checks.json"):
                (self.run_dir / "baseline-checks.json").write_text(
                    self.cache.read("baseline-checks.json"), encoding="utf-8")
                s.emit("cache_hit", {"artifact": "baseline-checks"})
                return
            data = run_ladder(Path(self.repo), self.ladder)
            (self.run_dir / "baseline-checks.json").write_text(json.dumps(data, indent=2))
            self.cache.write_json("baseline-checks.json", data)
            if data.get("empty"):
                # Empty ladder → warn loudly; a run with no checks has no objective gate.
                s.set(checks_empty=True)
                s.emit("warning", {"msg": "check ladder is EMPTY — no objective gate"})

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(do_probe), ex.submit(do_baseline)
            f1.result()
            f2.result()

        self._ensure_ladder()
        s.add_timing("INTAKE", machine_s=time.time() - t0)
        s.emit("stage_completed", {"stage": "INTAKE"})
        self.set_flag("intake_done")
        s.set(phase="TRIAGE")

    def _ensure_ladder(self) -> None:
        if not self.ladder:
            override = Path(self._opts["checks_file"]) if self._opts["checks_file"] else None
            self.ladder = detect_ladder(Path(self.repo), override)

    # ------------------------------------------------------------------
    # 1 · TRIAGE

    def stage_triage(self) -> None:
        s = self.state
        s.emit("stage_started", {"stage": "TRIAGE"})
        forced = self._opts.get("force_route")
        if forced in ("fasttrack", "full"):
            s.emit("triage", {"route": forced, "reasoning": "forced via --route"})
            s.set(path=forced, phase="FASTTRACK" if forced == "fasttrack" else "RECON")
            s.emit("stage_completed", {"stage": "TRIAGE"})
            return
        probe_md = self.read_artifact("probe.md")
        res = dispatch(s, self.cfg, name="triage", stage="TRIAGE", tier="cheap",
                       workdir=self.repo, prompt=P.triage(self.request, probe_md))
        route, reasoning = "full", "triage output unparseable — full on any uncertainty"
        try:
            m = re.search(r"\{.*\}", res.text, re.S)
            if m:
                data = json.loads(m.group(0))
                if data.get("route") in ("fasttrack", "full"):
                    route, reasoning = data["route"], data.get("reasoning", "")
        except json.JSONDecodeError:
            pass
        s.emit("triage", {"route": route, "reasoning": reasoning})
        if route == "fasttrack":
            s.set(path="fasttrack", phase="FASTTRACK")
        else:
            s.set(path="full", phase="RECON")
        s.emit("stage_completed", {"stage": "TRIAGE"})

    # ------------------------------------------------------------------
    # FASTTRACK (side path)

    def stage_fasttrack(self) -> None:
        s = self.state
        s.set(phase="FASTTRACK")
        s.emit("stage_started", {"stage": "FASTTRACK"})
        wt, br = W.add_worktree(s, self.repo, "fasttrack", self.sha)
        feedback = ""
        while True:
            res = dispatch(s, self.cfg, name=f"fasttrack-{s.get_counter('ft_attempts')}",
                           stage="FASTTRACK", tier="strong", workdir=wt,
                           prompt=P.fasttrack(self.request, self.read_artifact("probe.md"),
                                              str(wt), feedback=feedback))
            verdict, rest = parse_verdict(res.text, ["DONE", "ESCALATE", "BLOCKED"])
            s.append("verdicts", {"stage": "FASTTRACK", "verdict": verdict, "detail": rest[:500]})
            if verdict == "DONE":
                s.counter("ft_attempts")
                W.commit_all(wt, f"g2 fasttrack: {rest.splitlines()[0][:100] if rest else 'change'}")
                s.emit("chain", {"id": "fasttrack", "update": {"status": "done"}})
                self.set_flag("fasttrack_done")
                s.set(phase="INTEGRATE")
                s.emit("stage_completed", {"stage": "FASTTRACK"})
                return
            if verdict == "ESCALATE":
                # ESCALATE matters more than DONE: discard the worktree, re-enter at RECON.
                n = s.counter("fasttrack_escalations")
                if n > self.cfg["caps"]["fasttrack_escalate"]:
                    raise Halt("fasttrack escalate", "FASTTRACK",
                               f"escalated {n} times: {rest}", "plan wrong")
                s.emit("fasttrack_escalated", {"reason": rest})
                W.remove_worktree(s, self.repo, "fasttrack", delete_branch=True)
                s.set(path="full", phase="RECON")
                self.set_flag("fasttrack_done")  # leave this path
                return
            # BLOCKED or unparseable
            raise Halt("agent blocked", "FASTTRACK",
                       rest or res.error or "no verdict", "code wrong",
                       where_to_look=[str(wt)])

    # ------------------------------------------------------------------
    # 2 · RECON

    def stage_recon(self) -> None:
        s = self.state
        s.emit("stage_started", {"stage": "RECON"})
        findings_path = self.run_dir / "recon" / "findings.md"
        cached_repo_section = self.cache.read("findings-repo.md") if self.cache else None
        if cached_repo_section:
            s.emit("cache_hit", {"artifact": "findings-repo"})
        baseline_checks = self.read_artifact("baseline-checks.json") or "(no baseline checks)"
        # Recon is read-only on the repo: snapshot before, compare after.
        status_before = R.git(self.repo, "status", "--porcelain")
        res = dispatch(s, self.cfg, name="recon", stage="RECON", tier="strongest",
                       workdir=self.repo,
                       prompt=P.recon(self.request, self.read_artifact("probe.md"),
                                      baseline_checks, cached_repo_section,
                                      str(findings_path)))
        if R.git(self.repo, "status", "--porcelain") != status_before:
            raise Halt("read-only violation", "RECON",
                       "recon modified the working tree", "code wrong")
        if not findings_path.exists():
            # fall back to the agent's reply text as findings
            findings_path.parent.mkdir(parents=True, exist_ok=True)
            findings_path.write_text(res.text or "(recon produced no findings)",
                                     encoding="utf-8")

        # split + cache the repo section (§5.1)
        findings = findings_path.read_text(encoding="utf-8")
        repo_sec, _task_sec = split_findings(findings)
        if repo_sec and self.cache:
            self.cache.write("findings-repo.md", repo_sec)

        # classification: recon's own verdict wins; low confidence → discovery
        _v, rest = parse_verdict(res.text, ["DONE"])
        cls = re.search(r"CLASSIFICATION:\s*(execution|discovery)", res.text, re.I)
        conf = re.search(r"CONFIDENCE:\s*(high|low)", res.text, re.I)
        classification = cls.group(1).lower() if cls else "execution"
        confidence = conf.group(1).lower() if conf else "low"
        if classification not in findings.lower() and "classification" not in findings.lower():
            pass  # coverage gate will judge the file, not us
        s.emit("classification", {"class": classification, "confidence": confidence})
        s.set(classification=classification)
        self.set_flag("recon_done")
        s.set(phase="COVERAGE_CHECK")
        s.emit("stage_completed", {"stage": "RECON"})

    # ------------------------------------------------------------------
    # 3 · COVERAGE_CHECK

    _CITE_RE = re.compile(r"[\w./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|c|cc|cpp|h|hpp|"
                          r"cs|css|html|json|ya?ml|toml|sql|sh|md|txt)(?::\d+(?:-\d+)?)?")

    def stage_coverage_check(self) -> None:
        """Computer gate: keyword-hit dirs accounted for, exclusions reasoned,
        citations resolve. Gap agents (strong) dispatch concurrently per gap."""
        s = self.state
        s.emit("stage_started", {"stage": "COVERAGE_CHECK"})
        findings = self.read_artifact("recon/findings.md")

        # every citation resolves (computer check at baseline). Only tokens that
        # look like repo paths (contain a slash) count — bare filenames are prose.
        bad = []
        checked = 0
        for m in self._CITE_RE.finditer(findings):
            anchor = m.group(0).replace("\\", "/").lstrip("./")
            if "/" not in anchor.split(":")[0]:
                continue
            checked += 1
            if R.resolve_cite(self.repo, self.sha, anchor) is None:
                bad.append(anchor)
        bad = sorted(set(bad))
        s.emit("coverage_citations", {"checked": checked, "unresolvable": bad})
        if len(bad) >= 3:  # unresolvable citations → HALT (§4.3)
            raise Halt("citation resolution", "COVERAGE_CHECK",
                       f"{len(bad)} citations in findings.md do not resolve, "
                       f"e.g. {bad[:5]}", "plan wrong",
                       where_to_look=[str(self.run_dir / "recon" / "findings.md")])

        # keyword-hit directories must be accounted for
        probe = json.loads(self.read_artifact("probe.json") or "{}")
        hit_dirs = set()
        for _kw, dirs in (probe.get("keyword_hits") or {}).items():
            for d, n in dirs.items():
                if n >= 3:  # significant
                    hit_dirs.add(d or ".")
        rounds_with_relevant = s.get_counter("coverage_relevant_rounds")
        while True:
            unaccounted = [d for d in sorted(hit_dirs)
                           if d != "." and d not in findings]
            if not unaccounted:
                break
            jobs = []
            for d in unaccounted:
                kw, hits = self._top_keyword(probe, d)
                jobs.append(dict(name=f"gap-{d.replace('/', '_')}-{rounds_with_relevant}",
                                 stage="COVERAGE_CHECK", tier="strong", workdir=self.repo,
                                 prompt=P.gap_agent(self.request, d, kw, hits, self.repo)))
            results = dispatch_parallel(s, self.cfg, jobs)
            relevant_found = False
            additions = []
            for d, res in zip(unaccounted, results):
                verdict, rest = parse_verdict(res.text, ["RELEVANT", "EXCLUDED"])
                if verdict == "RELEVANT":
                    relevant_found = True
                    additions.append(f"- `{d}` — RELEVANT (gap agent): {rest[:800]}")
                else:
                    additions.append(f"- `{d}` — *excluded* (gap agent): {rest[:300]}")
            with (self.run_dir / "recon" / "findings.md").open("a", encoding="utf-8") as f:
                f.write("\n\n### Coverage gap resolution (appended by COVERAGE_CHECK)\n"
                        + "\n".join(additions) + "\n")
            findings = self.read_artifact("recon/findings.md")
            if relevant_found:
                rounds_with_relevant = s.counter("coverage_relevant_rounds")
                s.emit("coverage_gap_found", {"dirs": unaccounted})
                # gap agent reporting materially relevant code missed twice → HALT
                if rounds_with_relevant >= self.cfg["caps"]["coverage_gap"]:
                    raise Halt("coverage gap", "COVERAGE_CHECK",
                               f"gap agents found materially relevant code recon missed, "
                               f"in {rounds_with_relevant} rounds: {unaccounted}",
                               "plan wrong",
                               where_to_look=[str(self.run_dir / "recon" / "findings.md")])
            else:
                break
        self.set_flag("coverage_done")
        s.emit("stage_completed", {"stage": "COVERAGE_CHECK"})
        # route discovery-shaped work
        if s.state.get("classification") == "discovery":
            s.set(path="discovery", phase="DISCOVERY")
        else:
            s.set(phase="CLARIFY")

    def _top_keyword(self, probe: dict, directory: str) -> tuple[str, int]:
        best, n = "", 0
        for kw, dirs in (probe.get("keyword_hits") or {}).items():
            if dirs.get(directory, 0) > n:
                best, n = kw, dirs[directory]
        return best or "the request", max(n, 1)

    # ------------------------------------------------------------------
    # DISCOVERY (side path)

    def stage_discovery(self) -> None:
        s = self.state
        s.set(phase="DISCOVERY")
        s.emit("stage_started", {"stage": "DISCOVERY"})
        wt, _br = W.add_worktree(s, self.repo, "discovery", self.sha)
        res = dispatch(s, self.cfg, name="discovery", stage="DISCOVERY", tier="strongest",
                       workdir=wt,
                       prompt=P.discovery(self.request,
                                          self.read_artifact("recon/findings.md"), str(wt)),
                       wall_s=self.cfg.get("max_wall_seconds", 1800) * 3)  # long leash
        verdict, rest = parse_verdict(res.text, ["FOUND", "FIXED", "BLOCKED"])
        s.append("verdicts", {"stage": "DISCOVERY", "verdict": verdict, "detail": rest[:500]})
        if verdict == "FIXED":
            W.commit_all(wt, f"g2 discovery: {rest[:100]}")
            s.emit("chain", {"id": "discovery", "update": {"status": "done"}})
            self.set_flag("discovery_done")
            s.set(phase="INTEGRATE")
            return
        if verdict == "FOUND":
            # The remaining fix is usually execution-shaped: record the cause,
            # discard the scratch worktree, re-enter the full path with it.
            s.emit("discovery_found", {"cause": rest})
            with (self.run_dir / "recon" / "findings.md").open("a", encoding="utf-8") as f:
                f.write(f"\n\n### Discovery agent found the cause\n{rest}\n")
            if not R.git(wt, "status", "--porcelain").strip():
                W.remove_worktree(s, self.repo, "discovery", delete_branch=True)
            else:
                W.commit_all(wt, "g2 discovery: investigation artifacts")
            s.set(path="full", phase="CLARIFY", classification="execution")
            self.set_flag("discovery_done")
            return
        raise Halt("agent blocked", "DISCOVERY", rest or res.error or "no verdict",
                   "code wrong", where_to_look=[str(wt)])

    # ------------------------------------------------------------------
    # 4 · CLARIFY

    def stage_clarify(self, extra_context: str = "") -> None:
        s = self.state
        s.set(phase="CLARIFY")
        s.emit("stage_started", {"stage": "CLARIFY"})
        findings = self.read_artifact("recon/findings.md")
        if extra_context:
            findings += f"\n\n### Review/judge defects to surface to the user\n{extra_context}\n"
        questions_path = self.run_dir / "questions.md"
        res = dispatch(s, self.cfg,
                       name=f"clarify-{s.get_counter('clarify_rounds')}",
                       stage="CLARIFY", tier="strong", workdir=self.repo,
                       prompt=P.clarify(self.request, findings, str(questions_path),
                                        s.state.get("assumptions")))
        s.counter("clarify_rounds")
        if not questions_path.exists():
            questions_path.write_text(res.text or "## Assumptions\n(none)\n", encoding="utf-8")
        qtext = questions_path.read_text(encoding="utf-8")

        # harvest [ASSUMPTION] defaults (surfaced again at APPLY)
        for m in re.finditer(r"\[ASSUMPTION\]\s*(.*)", qtext):
            s.append("assumptions", m.group(1).strip())

        n_blocking = len(re.findall(r"\[BLOCKING\]", qtext))
        answers_path = self.run_dir / "answers.md"
        if n_blocking == 0:
            answers_path.write_text(
                "# Answers\n\nNo blocking questions were raised.\n\n" + qtext, encoding="utf-8")
        elif self.answers_file and Path(self.answers_file).exists():
            answers_path.write_text(Path(self.answers_file).read_text(encoding="utf-8"),
                                    encoding="utf-8")
        elif self.non_interactive:
            # Non-interactive: every blocking question falls back to an
            # [ASSUMPTION]-tagged default, surfaced at APPLY.
            answers_path.write_text(
                "# Answers (non-interactive defaults)\n\n" + qtext +
                "\n\nAll blocking questions were defaulted by the runner in "
                "non-interactive mode; review the [ASSUMPTION] list at APPLY.\n",
                encoding="utf-8")
            s.append("assumptions",
                     f"{n_blocking} blocking question(s) defaulted in non-interactive mode")
        elif not sys.stdin.isatty():
            # Headless (driven by an agent/TUI): publish the questions and wait
            # for answers.md to appear. Human latency is logged separately (§5.5).
            import sys
            if answers_path.exists():
                answers_path.unlink()
            s.emit("clarify_waiting", {"questions": str(questions_path),
                                       "answers": str(answers_path)})
            print(f"CLARIFY: {n_blocking} blocking question(s) in {questions_path}",
                  file=sys.stderr)
            print(f"CLARIFY: write answers to {answers_path} to continue",
                  file=sys.stderr)
            t0 = time.time()
            while not answers_path.exists():
                time.sleep(5)
            s.add_timing("CLARIFY", human_s=time.time() - t0)
            s.emit("clarify_answered", {"answers": str(answers_path)})
        else:
            # One batched interaction — human latency dwarfs everything else (§4.4).
            import sys
            print("\n" + "=" * 72, file=sys.stderr)
            print("CLARIFY needs answers before planning can continue.", file=sys.stderr)
            print(f"Questions are in: {questions_path}\n", file=sys.stderr)
            print(qtext, file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            print("Write your answers (any markdown; address Q1..Qn), then paste them",
                  file=sys.stderr)
            print("here. End input with a line containing only '###'.", file=sys.stderr)
            t0 = time.time()
            lines = []
            try:
                while True:
                    ln = input()
                    if ln.strip() == "###":
                        break
                    lines.append(ln)
            except EOFError:
                pass
            human_s = time.time() - t0
            s.add_timing("CLARIFY", human_s=human_s)
            answers_path.write_text("# Answers\n\n" + "\n".join(lines), encoding="utf-8")
        self.set_flag("clarify_done")
        s.set(phase="PROMPT_GEN")
        s.emit("stage_completed", {"stage": "CLARIFY"})

    # ------------------------------------------------------------------
    # 5 · PROMPT_GEN

    def stage_prompt_gen(self, only_chains: list[str] | None = None,
                         kickback: str = "") -> None:
        s = self.state
        s.set(phase="PROMPT_GEN")
        s.emit("stage_started", {"stage": "PROMPT_GEN", "only_chains": only_chains})
        plan_dir = self.run_dir / "plan"
        findings = self.read_artifact("recon/findings.md")
        answers = self.read_artifact("answers.md")

        skel_path = plan_dir / "skeleton.json"
        if only_chains is None and not (self.flag("skeleton_done") and skel_path.exists()):
            # 5a · Skeleton — whole-task context, cannot be parallelised.
            res = dispatch(s, self.cfg, name="skeleton", stage="PROMPT_GEN", tier="strongest",
                           workdir=self.repo,
                           prompt=P.skeleton(self.request, findings, answers,
                                             P.SKELETON_FORMAT, str(plan_dir)))
            if not skel_path.exists():
                raise Halt("artifact missing", "PROMPT_GEN",
                           f"skeleton agent did not write {skel_path}: "
                           f"{(res.text or res.error or '')[:400]}", "plan wrong")
            self.set_flag("skeleton_done")
        if only_chains is None:
            plan = self._skeleton_to_plan(skel_path)
        else:
            plan = Plan.load(plan_dir)

        # 5b · Bodies — one agent per chain, concurrent (§5.4).
        chains = only_chains or [c.id for c in plan.chains]
        interfaces = self.read_artifact("plan/interfaces.md")
        skel_json = (plan_dir / "skeleton.json").read_text(encoding="utf-8") \
            if (plan_dir / "skeleton.json").exists() else json.dumps(plan.to_dict())
        jobs = []
        for cid in chains:
            chain = plan.chain(cid)
            stub = json.dumps({"id": chain.id, "title": chain.title,
                               "depends_on": chain.depends_on,
                               "prompts": [
                                   {"id": p.id, "seq": p.seq, "title": p.title,
                                    "cites": p.cites, "touches": p.touches,
                                    "assumes": p.assumes, "records": p.records,
                                    "gate": p.gate}
                                   for p in chain.prompts]}, indent=2)
            fb = kickback if only_chains else ""
            jobs.append(dict(name=f"bodies-{cid}-{s.get_counter('body_rounds')}",
                             stage="PROMPT_GEN", tier="strongest", workdir=self.repo,
                             prompt=P.body_writer(self.request, findings, answers, skel_json,
                                                  interfaces, cid, stub,
                                                  str(plan_dir / f"bodies-{cid}.json"),
                                                  kickback_feedback=fb)))
        s.counter("body_rounds")
        dispatch_parallel(s, self.cfg, jobs)
        self._merge_bodies(plan, chains)

        # cite_hash: content hash of each anchor at plan time (pre-flight input, §4.7a)
        for p in plan.all_prompts():
            for anchor in p.cites:
                h = R.hash_cite_at(self.repo, self.sha, anchor)
                if h:
                    p.cite_hash[anchor] = h

        # decision registry: sync affects from assumes
        for p in plan.all_prompts():
            for did in p.assumes:
                d = plan.decisions.get(did)
                if d and p.id not in d.affects:
                    d.affects.append(p.id)
        plan.save(plan_dir)

        n = len(plan.all_prompts())
        s.emit("prompt_count", {"count": n})
        if n > self.cfg.get("prompt_count_warn", 20):
            s.emit("warning", {"msg": f"prompt count {n} > 20 — granularity rule (§4.5)"})

        # the skeleton agent's contract: validate-plan passes before we proceed
        for fix_round in range(2):
            errors = validate_plan(plan, self.repo, self.sha)
            if not errors:
                break
            s.emit("validate_plan_failed", {"errors": errors, "fix_round": fix_round})
            self._plan_fix(plan, errors)
        errors = validate_plan(plan, self.repo, self.sha)
        if errors:
            raise Halt("validate-plan", "PROMPT_GEN",
                       "plan failed validation after fix rounds: " + "; ".join(errors[:5]),
                       "plan wrong", where_to_look=[str(plan_dir / "plan.json")])
        self.set_flag("promptgen_done")
        s.set(phase="REVIEW")
        s.emit("stage_completed", {"stage": "PROMPT_GEN"})

    def _skeleton_to_plan(self, skel_path: Path) -> Plan:
        from .planmodel import Chain, Decision, Prompt
        raw = json.loads(skel_path.read_text(encoding="utf-8"))
        plan = Plan()
        plan.interfaces = self.read_artifact("plan/interfaces.md")
        plan.rationale = self.read_artifact("plan/rationale.md")
        for c in raw.get("chains", []):
            chain = Chain(id=c["id"], title=c.get("title", ""),
                          depends_on=list(c.get("depends_on", [])))
            for p in c.get("prompts", []):
                chain.prompts.append(Prompt(
                    id=p["id"], chain=chain.id, seq=int(p.get("seq", 0)),
                    title=p.get("title", ""), cites=list(p.get("cites", [])),
                    touches=list(p.get("touches", [])),
                    assumes=list(p.get("assumes", [])),
                    records=list(p.get("records", [])),
                    gate=p.get("gate", "none")))
            plan.chains.append(chain)
        for d in raw.get("decisions", []):
            plan.decisions[d["id"]] = Decision(
                id=d["id"], question=d.get("question", ""),
                assumed_value=str(d.get("assumed_value", "")),
                decided_by=d.get("decided_by", ""),
                affects=list(d.get("affects", [])))
        plan.save(self.run_dir / "plan")
        return plan

    def _merge_bodies(self, plan: Plan, chain_ids: list[str]) -> None:
        from .planmodel import Decision
        plan_dir = self.run_dir / "plan"
        for cid in chain_ids:
            bp = plan_dir / f"bodies-{cid}.json"
            if not bp.exists():
                raise Halt("artifact missing", "PROMPT_GEN",
                           f"body writer for chain {cid} did not write {bp.name}",
                           "plan wrong")
            raw = json.loads(bp.read_text(encoding="utf-8"))
            chain = plan.chain(cid)
            by_id = {p["id"]: p for p in raw.get("prompts", [])
                     if isinstance(p, dict) and "id" in p}
            for p in chain.prompts:
                b = by_id.get(p.id)
                if not b:
                    continue
                p.body = b.get("body", p.body)
                p.cites = list(b.get("cites", p.cites))
                # hashes belong to the previous version of this prompt — drop them;
                # the caller's re-hash loop repopulates for the new cites.
                p.cite_hash = {}
                p.touches = list(b.get("touches", p.touches))
                p.acceptance = list(b.get("acceptance", p.acceptance))
                p.assumes = list(b.get("assumes", p.assumes))
                p.records = list(b.get("records", p.records))
                p.gate = b.get("gate", p.gate)
            for d in raw.get("decisions", []):
                if d["id"] not in plan.decisions:
                    plan.decisions[d["id"]] = Decision(
                        id=d["id"], question=d.get("question", ""),
                        assumed_value=str(d.get("assumed_value", "")),
                        decided_by=d.get("decided_by", ""),
                        affects=list(d.get("affects", [])))

    def _plan_fix(self, plan: Plan, errors: list[str]) -> None:
        """Skeleton agent fixes validate-plan failures before we report (§4.5)."""
        s = self.state
        plan_dir = self.run_dir / "plan"
        fix_prompt = f"""You are the PROMPT_GEN skeleton agent in a fix pass. The plan
you produced failed the computer-run validator. Fix the plan artifacts so it passes.

# Validation failures
{chr(10).join('- ' + e for e in errors)}

# The plan files (fix them in place)
    {plan_dir}/skeleton.json
    {plan_dir}/interfaces.md
    {plan_dir}/rationale.md
Bodies live in {plan_dir}/bodies-<chain>.json — fix those too if the failure is in a
body (bad gate value, dangling assumes id, overlapping touches within a wave, cites
that do not resolve at the repo baseline).

Field names are contractual: Prompt(id, chain, seq, title, body, cites, cite_hash,
touches, acceptance, assumes, records, gate); Decision(id, question, assumed_value,
actual_value, decided_by, affects); Chain(id, title, prompts, depends_on).

# Verdict (final message, first line)
FIXED: <what you changed>
"""
        dispatch(s, self.cfg, name=f"planfix-{s.get_counter('planfix')}", stage="PROMPT_GEN",
                 tier="strongest", workdir=self.repo, prompt=fix_prompt)
        s.counter("planfix")
        # reload from disk after the fix
        skel = plan_dir / "skeleton.json"
        if skel.exists():
            fresh = self._skeleton_to_plan(skel)
            for cid in [c.id for c in fresh.chains]:
                bp = plan_dir / f"bodies-{cid}.json"
                if bp.exists():
                    self._merge_bodies(fresh, [cid])
            plan.chains, plan.decisions = fresh.chains, fresh.decisions
            plan.interfaces, plan.rationale = fresh.interfaces, fresh.rationale
            for p in plan.all_prompts():
                for anchor in p.cites:
                    h = R.hash_cite_at(self.repo, self.sha, anchor)
                    if h:
                        p.cite_hash[anchor] = h
            plan.save(plan_dir)

    # ------------------------------------------------------------------
    # 6 · REVIEW (6a plan ∥ 6b attack)

    def stage_review(self) -> None:
        s = self.state
        s.set(phase="REVIEW")
        s.emit("stage_started", {"stage": "REVIEW"})
        plan = Plan.load(self.run_dir / "plan")

        # computer pre-check before both agents
        errors = validate_plan(plan, self.repo, self.sha)
        if errors:
            raise Halt("validate-plan", "REVIEW",
                       "plan failed pre-review validation: " + "; ".join(errors[:5]),
                       "plan wrong")

        chain_titles = "\n".join(f"- {c.id}: {c.title} (depends_on: {c.depends_on})"
                                 for c in plan.chains)
        registry = "\n".join(
            f"- {d.id}: {d.question} → assumed: {d.assumed_value} "
            f"(decided_by {d.decided_by}, affects {d.affects})"
            for d in plan.decisions.values()) or "(no decisions)"
        coverage = self._coverage_statement()
        rationale = self.read_artifact("plan/rationale.md")

        # 6b: prompts batched across concurrent attackers in groups of five (§4.6b)
        all_p = plan.all_prompts()
        attack_jobs = []
        for i in range(0, len(all_p), 5):
            batch = all_p[i:i + 5]
            batch_text = "\n\n---\n\n".join(
                f"### {p.id} (chain {p.chain}, seq {p.seq}): {p.title}\n"
                f"cites: {p.cites}\ntouches: {p.touches}\nassumes: {p.assumes}\n"
                f"records: {p.records}\ngate: {p.gate}\n\n{p.body}\n\n"
                f"acceptance:\n" + "\n".join(f"- {a}" for a in p.acceptance)
                for p in batch)
            attack_jobs.append(dict(name=f"attack-{i // 5}", stage="REVIEW", tier="strong",
                                    workdir=self.repo, prompt=P.attack(batch_text)))
        review_job = dict(name="plan-review", stage="REVIEW", tier="strongest",
                          workdir=self.repo,
                          prompt=P.plan_review(self.request, rationale, chain_titles,
                                               registry, coverage))
        results = dispatch_parallel(s, self.cfg, [review_job] + attack_jobs)

        rv, rrest = parse_verdict(results[0].text, ["PASS", "REJECT-PLAN"])
        s.append("verdicts", {"stage": "REVIEW-6a", "verdict": rv, "detail": rrest[:800]})
        kickbacks = []
        for r in results[1:]:
            v, rest = parse_verdict(r.text, ["PASS", "KICKBACK"])
            s.append("verdicts", {"stage": "REVIEW-6b", "verdict": v, "detail": rest[:800]})
            if v == "KICKBACK":
                kickbacks.append(rest)

        if rv == "REJECT-PLAN":
            n_rejects = s.counter("plan_review_rejects")
            if n_rejects > self.cfg["caps"]["plan_reject"]:
                raise Halt("plan review", "REVIEW",
                           f"plan rejected {n_rejects} times; "
                           f"final defects: {rrest[:600]}", "plan wrong")
            # surface defects to the user first, then re-plan (§4.6 routing)
            self.set_flag("clarify_done", False)
            self.set_flag("promptgen_done", False)
            self.stage_clarify(extra_context=rrest)
            self.stage_prompt_gen()
            return self.stage_review()

        if kickbacks:
            n = s.counter("kickback_rounds")
            if n > self.cfg["caps"]["kickback"]:
                raise Halt("attack", "REVIEW",
                           f"kickback cap exhausted; remaining defects: "
                           f"{kickbacks[-1][:600]}", "plan wrong")
            affected = sorted({m.group(1)
                               for kb in kickbacks
                               for m in re.finditer(r"\[(p\d+)\]", kb)})
            chains = sorted({plan.prompt(pid).chain for pid in affected
                             if plan.prompt(pid)}) or [c.id for c in plan.chains]
            self.set_flag("promptgen_done", False)
            self.stage_prompt_gen(only_chains=chains,
                                  kickback="\n\n".join(kickbacks))
            return self.stage_review()

        self.set_flag("review_done")
        s.set(phase="EXECUTE")
        s.emit("stage_completed", {"stage": "REVIEW"})

    def _coverage_statement(self) -> str:
        findings = self.read_artifact("recon/findings.md")
        m = re.search(r"coverage statement(.*?)(\n#|\Z)", findings, re.I | re.S)
        return m.group(1).strip() if m else findings[-2000:]

    # ------------------------------------------------------------------
    # 7 · EXECUTE

    def stage_execute(self) -> None:
        s = self.state
        s.set(phase="EXECUTE")
        s.emit("stage_started", {"stage": "EXECUTE"})
        plan = Plan.load(self.run_dir / "plan")
        waves = plan.waves()
        s.emit("waves", {"waves": waves})
        single_chain = len(plan.chains) == 1

        for wave in waves:
            # chains within a wave run concurrently: disjoint touches, separate
            # worktrees (§5.4). Attempt tracking is per chain, never global.
            with ThreadPoolExecutor(max_workers=len(wave)) as ex:
                futs = {ex.submit(self._run_chain, plan, cid, single_chain): cid
                        for cid in wave}
                for f in futs:
                    f.result()  # chain failures are recorded, not raised

        failed = [cid for cid, c in s.state["chains"].items()
                  if c.get("status") in ("failed", "stale-halted")]
        if failed:
            raise Halt("chain failure", "EXECUTE",
                       f"chains failed: {failed}; see journal for per-chain reasons",
                       "code wrong")
        self.set_flag("execute_done")
        s.set(phase="INTEGRATE")
        s.emit("stage_completed", {"stage": "EXECUTE"})

    def _run_chain(self, plan: Plan, cid: str, single_chain: bool) -> None:
        s = self.state
        if s.state["chains"].get(cid, {}).get("status") == "done":
            return  # already executed (e.g. re-entry after REJECT-PLAN)
        chain = plan.chain(cid)
        s.emit("chain", {"id": cid, "update": {"status": "running"}})
        t0 = time.time()
        max_wall = self.cfg.get("max_wall_seconds", 1800)
        wt, br = W.add_worktree(s, self.repo, cid, self.sha)
        prefix = self._executor_prefix(plan, chain)
        done_prompts = set(s.state["chains"].get(cid, {}).get("prompts_done", []))

        try:
            idx = 0
            while idx < len(chain.prompts):
                p = chain.prompts[idx]
                if p.id in done_prompts:
                    idx += 1
                    continue
                if time.time() - t0 > max_wall:
                    s.emit("chain", {"id": cid, "update": {
                        "status": "failed", "reason": f"over max_wall_seconds ({max_wall})"}})
                    return

                # a. citation pre-flight (computer): re-hash every anchor against the
                # worktree's current state; deterministic detection beats hoping.
                stale = [a for a, h in p.cite_hash.items()
                         if R.cite_hash_in_tree(wt, a) != h]
                if stale:
                    s.emit("preflight_stale", {"prompt": p.id, "anchors": stale})
                    if not self._replan(plan, chain, idx,
                                        f"cited code changed since planning: {stale}",
                                        wt=wt):
                        s.emit("chain", {"id": cid, "update": {
                            "status": "stale-halted", "reason": "replan cap (stale cites)"}})
                        return
                    continue  # re-enter with rewritten remainder

                # b. inject actual decision values (not assumed values)
                dec_values = {}
                for did in p.assumes:
                    rec = s.state["decisions"].get(did, {})
                    d = plan.decisions.get(did)
                    dec_values[did] = rec.get("actual_value") or (
                        d.assumed_value if d else "(unknown)")
                # c. dispatch — cheap model, stable prefix + varying tail (§5.3)
                exec_count = s.get_counter(f"exec:{p.id}")
                res = dispatch(s, self.cfg, name=f"exec-{p.id}-{exec_count}",
                               stage="EXECUTE", tier="cheap", workdir=wt,
                               prompt=P.executor(prefix, p.body, p.id, p.title,
                                                 p.acceptance, dec_values, p.records))
                s.counter(f"exec:{p.id}")
                verdict, rest = parse_verdict(res.text, ["DONE", "BLOCKED"])
                if res.timed_out or res.stalled:
                    s.emit("chain", {"id": cid, "update": {
                        "status": "failed",
                        "reason": f"{p.id} {'timed out' if res.timed_out else 'stalled'}"}})
                    return  # over-budget or stalled → record failure, do not block the wave
                if verdict != "DONE":
                    s.emit("chain", {"id": cid, "update": {
                        "status": "failed", "reason": f"{p.id}: {rest[:300]}"}})
                    return

                # d. record actual decision values
                W.commit_all(wt, f"g2 {p.id}: {p.title}")
                records = parse_records(res.text)
                for did in p.records:
                    actual = records.get(did)
                    d = plan.decisions.get(did)
                    if actual is None and d is not None:
                        actual = d.assumed_value  # executor silently took the default
                    s.emit("decision", {"id": did, "update": {
                        "actual_value": actual, "decided_by": p.id, "recorded": True}})
                    if d is not None:
                        d.actual_value = actual

                # e. divergence check — actual vs assumed; diverge → remainder STALE
                diverged = [did for did in p.records
                            if plan.decisions.get(did)
                            and s.state["decisions"].get(did, {}).get("actual_value")
                            not in (None, plan.decisions[did].assumed_value)]
                if diverged:
                    affected = [pid for did in diverged
                                for pid in (plan.decisions[did].affects if plan.decisions.get(did) else [])]
                    remaining = [q.id for q in chain.prompts[idx + 1:]]
                    if set(affected) & set(remaining):
                        s.emit("divergence", {"prompt": p.id, "decisions": diverged})
                        if not self._replan(plan, chain, idx + 1,
                                            f"decisions diverged from assumptions: "
                                            f"{diverged}", wt=wt):
                            s.emit("chain", {"id": cid, "update": {
                                "status": "stale-halted",
                                "reason": "replan cap (divergence)"}})
                            return
                        done_prompts.add(p.id)
                        s.emit("chain", {"id": cid, "update": {
                            "prompts_done": sorted(done_prompts)}})
                        idx += 1
                        continue

                # f. chain gate — ladder under §5.2 cost controls
                if p.gate == "chain" and not single_chain:
                    gate_ok = self._chain_gate(plan, chain, wt, br)
                    if not gate_ok:
                        s.emit("chain", {"id": cid, "update": {
                            "status": "failed", "reason": f"chain gate failed at {p.id}"}})
                        return

                done_prompts.add(p.id)
                s.emit("chain", {"id": cid, "update": {"prompts_done": sorted(done_prompts)}})
                idx += 1

            W.commit_all(wt, f"g2 {cid}: chain complete")
            s.emit("chain", {"id": cid, "update": {"status": "done"}})
        except Exception as e:  # noqa: BLE001 — record failure, move on (§4.7)
            s.emit("chain", {"id": cid, "update": {"status": "failed", "reason": str(e)}})

    def _executor_prefix(self, plan: Plan, chain) -> str:
        """Stable prefix: interface contract, conventions excerpt, chain summary.
        Identical across a chain so prompt caching holds (§5.3)."""
        repo_sec, _ = split_findings(self.read_artifact("recon/findings.md"))
        conventions = repo_sec or self.read_artifact("recon/findings.md")[:3000]
        prompts_line = "\n".join(f"  {p.seq}. {p.id} — {p.title}" for p in chain.prompts)
        return f"""You are an EXECUTOR in an agent pipeline. You are deliberately cheap:
the prompts you receive were written so you need no repo knowledge beyond what they
provide. Work only inside your current directory (a dedicated git worktree).

# Interface contract (plan/interfaces.md)
{plan.interfaces or "(none)"}

# Repo conventions (from recon)
{conventions}

# Your chain: {chain.id} — {chain.title}
{prompts_line}
Prompts in this chain run in order; earlier ones have already been executed and
their edits are in your worktree.
"""

    def _chain_gate(self, plan: Plan, chain, wt: Path, br: str) -> bool:
        """Chain gate: never e2e (§5.2); impact-selected tests where possible."""
        s = self.state
        self._ensure_ladder()
        probe = json.loads(self.read_artifact("probe.json") or "{}")
        changed = R.changed_files(self.repo, self.sha, br)
        only, mode = select_impacted_tests(probe, changed)
        if only == []:  # no tests reach the change — nothing meaningful to run
            s.emit("chain_gate", {"chain": chain.id, "mode": mode, "ok": True})
            return True
        result = run_ladder(wt, self.ladder, kinds={"typecheck", "lint", "unit"},
                            only_tests=only)
        s.emit("chain_gate", {"chain": chain.id, "mode": mode,
                              "ok": result["ok"], "checks": [
                                  {"name": c["name"], "exit": c["exit"]}
                                  for c in result["checks"]]})
        return result["ok"]

    def _replan(self, plan: Plan, chain, from_idx: int, reason: str,
                wt: Path | None = None) -> bool:
        """Route a chain remainder back to PROMPT_GEN 5b (§4.7e). The prompter
        rewrites only the remainder; completed prompts and their edits stand.
        Replanned prompts re-enter attack (6b) before executing. Cap 2 per chain.

        `wt` is the chain worktree: re-hashed citations must be taken against its
        CURRENT state (which includes completed prompts' edits), not the baseline —
        hashing against baseline would make every replanned prompt instantly stale."""
        s = self.state
        n = s.counter(f"replan:{chain.id}")
        if n > self.cfg["caps"]["replan_per_chain"]:
            return False
        s.emit("replan", {"chain": chain.id, "from": chain.prompts[from_idx].id
                          if from_idx < len(chain.prompts) else "end",
                          "reason": reason, "round": n})
        remainder = chain.prompts[from_idx:]
        if not remainder:
            return True
        findings = self.read_artifact("recon/findings.md")
        stub = json.dumps({"id": chain.id, "title": chain.title,
                           "prompts": [{"id": p.id, "seq": p.seq, "title": p.title}
                                       for p in remainder]}, indent=2)
        replan_prompt = f"""You are PROMPT_GEN phase 5b rewriting the REMAINDER of chain
`{chain.id}` after execution hit reality. Completed prompts and their edits stand —
rewrite only these prompts: {[p.id for p in remainder]}.

# Why the remainder went stale
{reason}

# recon/findings.md
{findings}

# Interface contract
{plan.interfaces or '(none)'}

# Current state note
The chain worktree at
    {wt}
has the completed prompts' edits applied. Read files THERE when writing and citing
the remainder — your citations will be hashed against that worktree's current state,
not the original baseline.

# Prompt stubs to rewrite
{stub}

# Rules
Same rules as any 5b body: executable by a cheap model with no repo knowledge, quote
real code verified right now, defect before fix, concrete acceptance, declare
decisions, full prose.

# Output
Write JSON at this exact absolute path:
    {self.run_dir}/plan/bodies-{chain.id}.json
Shape: {{"chain": "{chain.id}", "prompts": [{{"id", "body", "cites", "touches",
"acceptance", "assumes", "records", "gate"}}], "decisions": []}}

# Verdict (final message, first line)
DONE: <n> prompts rewritten
"""
        dispatch(s, self.cfg, name=f"replan-{chain.id}-{n}", stage="EXECUTE",
                 tier="strongest", workdir=self.repo, prompt=replan_prompt)
        self._merge_bodies(plan, [chain.id])
        for p in chain.prompts:
            for anchor in p.cites:
                h = (R.cite_hash_in_tree(wt, anchor) if wt is not None
                     else R.hash_cite_at(self.repo, self.sha, anchor))
                if h:
                    p.cite_hash[anchor] = h
        plan.save(self.run_dir / "plan")

        # replanned prompts re-enter attack before executing (§4.7e)
        batch_text = "\n\n---\n\n".join(
            f"### {p.id} (chain {p.chain}, seq {p.seq}): {p.title}\n"
            f"cites: {p.cites}\ntouches: {p.touches}\nassumes: {p.assumes}\n"
            f"records: {p.records}\ngate: {p.gate}\n\n{p.body}\n\nacceptance:\n"
            + "\n".join(f"- {a}" for a in p.acceptance)
            for p in remainder)
        res = dispatch(s, self.cfg, name=f"attack-replan-{chain.id}-{n}", stage="EXECUTE",
                       tier="strong", workdir=self.repo, prompt=P.attack(batch_text))
        verdict, rest = parse_verdict(res.text, ["PASS", "KICKBACK"])
        s.append("verdicts", {"stage": "REVIEW-6b-replan", "verdict": verdict,
                              "detail": rest[:500]})
        if verdict == "KICKBACK" and n < self.cfg["caps"]["replan_per_chain"]:
            return self._replan(plan, chain, from_idx,
                                f"attack kicked back replanned prompts: {rest[:400]}",
                                wt=wt)
        return True

    # ------------------------------------------------------------------
    # 8 · INTEGRATE

    def stage_integrate(self) -> None:
        s = self.state
        s.set(phase="INTEGRATE")
        s.emit("stage_started", {"stage": "INTEGRATE"})
        self._ensure_ladder()
        path = s.state.get("path")
        if path in ("fasttrack", "discovery"):
            order = ["fasttrack" if path == "fasttrack" else "discovery"]
        else:
            plan = Plan.load(self.run_dir / "plan")
            order = [cid for wave in plan.waves() for cid in wave]
        order = [cid for cid in order
                 if s.state["chains"].get(cid, {}).get("status") == "done"]
        if not order:
            raise Halt("nothing to integrate", "INTEGRATE",
                       "no chain completed successfully", "code wrong")

        wt, result_br = W.add_worktree(s, self.repo, "result", self.sha)

        if len(order) == 1:
            # single-chain runs skip conflict detection — nothing to conflict with
            br = s.state["chains"][order[0]]["branch"]
            R.git(wt, "merge", "--no-edit", br)
            s.emit("merge", {"chain": order[0], "mode": "single", "conflict": False})
        else:
            import subprocess
            for cid in order:
                br = s.state["chains"][cid]["branch"]
                # merge-tree detects conflicts before the merge touches anything
                probe = subprocess.run(
                    ["git", "-C", str(wt), "merge-tree", "--write-tree", "HEAD", br],
                    capture_output=True, text=True)
                if probe.returncode == 0:
                    R.git(wt, "merge", "--no-edit", br)  # clean → zero AI involvement
                    s.emit("merge", {"chain": cid, "conflict": False})
                else:
                    R.git(wt, "merge", "--no-edit", br, check=False)
                    self._resolve_conflict(wt, cid, br)

        # full ladder (e2e included) + baseline delta — runs on all three paths
        result = run_ladder(wt, self.ladder,
                            out_json=self.run_dir / "integration-checks.json")
        baseline = json.loads(self.read_artifact("baseline-checks.json") or "{}")
        if baseline.get("skipped"):
            delta = {"hard_failure": False, "note": "baseline skipped"}
        else:
            delta = compute_delta(baseline, result)
        (self.run_dir / "delta.json").write_text(json.dumps(delta, indent=2))
        s.emit("baseline_delta", delta)
        if delta.get("hard_failure"):
            attribution = self._attribute(delta, order)
            raise Halt("baseline delta", "INTEGRATE",
                       f"regressions/new-failing: "
                       f"{delta.get('regression', []) + delta.get('new_failing', [])} "
                       f"— attribution: {attribution}", "code wrong",
                       where_to_look=[str(self.run_dir / "integration-checks.json")])
        self.set_flag("integrate_done")
        s.set(phase="JUDGE")
        s.emit("stage_completed", {"stage": "INTEGRATE"})

    def _resolve_conflict(self, wt: Path, cid: str, br: str) -> None:
        s = self.state
        s.emit("merge", {"chain": cid, "conflict": True})
        conflicted = R.git(wt, "diff", "--name-only", "--diff-filter=U").split()
        hunks = ""
        for f in conflicted:
            hunks += f"\n===== {f} =====\n"
            hunks += (wt / f).read_text(encoding="utf-8", errors="replace")[:6000]
        bodies = ""
        plan_file = self.run_dir / "plan" / "plan.json"
        if plan_file.exists():
            plan = Plan.load(self.run_dir / "plan")
            bodies = "\n\n".join(f"### {p.id}: {p.title}\n{p.body[:1500]}"
                                 for p in plan.all_prompts())
        registry = json.dumps(s.state.get("decisions", {}), indent=2)
        res = dispatch(s, self.cfg, name=f"merge-{cid}", stage="INTEGRATE", tier="strong",
                       workdir=wt, prompt=P.merge_agent(hunks, bodies, registry))
        verdict, rest = parse_verdict(res.text, ["RESOLVED", "INCOMPATIBLE"])
        if verdict != "RESOLVED":
            R.git(wt, "merge", "--abort", check=False)
            raise Halt("merge conflict", "INTEGRATE",
                       f"merge agent: {rest[:500]}", "plan wrong",
                       where_to_look=conflicted)
        if R.git(wt, "diff", "--name-only", "--diff-filter=U", check=False).strip():
            R.git(wt, "merge", "--abort", check=False)
            raise Halt("merge conflict", "INTEGRATE",
                       "merge agent left unresolved conflict markers", "code wrong",
                       where_to_look=conflicted)
        R.git(wt, "add", "-A")
        R.git(wt, "-c", "user.name=gigga2", "-c", "user.email=gigga2@localhost",
              "commit", "--no-edit")

    def _attribute(self, delta: dict, order: list[str]) -> dict:
        """Attribute regressions by intersecting failing test source paths with
        each chain's diff; unattributable → integration (§4.8)."""
        failing = delta.get("regression", []) + delta.get("new_failing", [])
        out: dict[str, list[str]] = {}
        chain_diffs = {}
        for cid in order:
            br = self.state.state["chains"].get(cid, {}).get("branch")
            if br:
                chain_diffs[cid] = set(R.changed_files(self.repo, self.sha, br))
        for tid in failing:
            fpath = tid.split("::")[0]
            owners = [cid for cid, files in chain_diffs.items()
                      if any(fpath in f or f in fpath for f in files)]
            out.setdefault(", ".join(owners) if owners else "integration", []).append(tid)
        return out

    # ------------------------------------------------------------------
    # 9 · JUDGE

    def stage_judge(self) -> None:
        s = self.state
        s.set(phase="JUDGE")
        s.emit("stage_started", {"stage": "JUDGE"})
        self._ensure_ladder()
        result_br = f"g2/{s.state['run_id']}/result"
        wt = self.run_dir / "worktrees" / "result"
        diff = R.git(self.repo, "diff", f"{self.sha}..{result_br}", check=False)
        (self.run_dir / "result.diff").write_text(diff, encoding="utf-8")
        checks = self.read_artifact("integration-checks.json")
        delta = self.read_artifact("delta.json")

        while True:
            res = dispatch(s, self.cfg, name=f"judge-{s.get_counter('judge_rounds')}",
                           stage="JUDGE", tier="strongest", workdir=self.repo,
                           prompt=P.judge(self.request, self.read_artifact("answers.md"),
                                          diff, checks + "\n\n# Delta\n" + delta,
                                          self._coverage_statement()))
            s.counter("judge_rounds")
            verdict, rest = parse_verdict(res.text, ["ACCEPT", "REJECT", "REJECT-PLAN"])
            s.append("verdicts", {"stage": "JUDGE", "verdict": verdict, "detail": rest[:800]})

            if verdict == "ACCEPT":
                self.set_flag("judge_done")
                s.set(phase="APPLY")
                s.emit("stage_completed", {"stage": "JUDGE"})
                return
            if verdict is None:
                raise Halt("judge", "JUDGE",
                           f"judge returned no verdict: {(res.error or res.text or '')[:300]}",
                           "code wrong")

            if verdict == "REJECT-PLAN":
                n = s.counter("judge_plan_rejects")
                total = s.get_counter("plan_review_rejects") + n
                if total > self.cfg["caps"]["plan_reject"]:
                    raise Halt("judge plan reject", "JUDGE",
                               f"plan rejected at the gates {total} times; final: "
                               f"{rest[:600]}", "plan wrong")
                # surface to the user, then re-plan (shares the 6a budget, §7)
                self.set_flag("clarify_done", False)
                self.set_flag("promptgen_done", False)
                self.set_flag("review_done", False)
                self.set_flag("execute_done", False)
                self.set_flag("integrate_done", False)
                self.stage_clarify(extra_context=rest)
                self.stage_prompt_gen()
                self.stage_review()
                self.stage_execute()
                self.stage_integrate()
                result_br = f"g2/{s.state['run_id']}/result"
                diff = R.git(self.repo, "diff", f"{self.sha}..{result_br}", check=False)
                checks = self.read_artifact("integration-checks.json")
                delta = self.read_artifact("delta.json")
                continue

            # REJECT → re-exec named prompts (or back to the fasttrack agent)
            ids = re.findall(r"\[(p\d+)\]", rest)
            if s.state.get("path") == "fasttrack":
                n = s.counter("fasttrack_rejects")
                if n > self.cfg["caps"]["fasttrack_reject"]:
                    # route change, not a failure: escalate to the full path (§4.9)
                    s.emit("fasttrack_escalated", {"reason": f"judge rejects x{n}: {rest[:300]}"})
                    s.set(path="full", phase="RECON")
                    self.set_flag("recon_done", False)
                    self.set_flag("coverage_done", False)
                    return self.run()
                s.append("attempts", f"judge REJECT → fasttrack retry {n}: {rest[:300]}")
                ft_wt = self.run_dir / "worktrees" / "fasttrack"
                res2 = dispatch(s, self.cfg, name=f"fasttrack-retry-{n}", stage="JUDGE",
                                tier="strong", workdir=ft_wt,
                                prompt=P.fasttrack(self.request,
                                                   self.read_artifact("probe.md"),
                                                   str(ft_wt), feedback=rest))
                v2, rest2 = parse_verdict(res2.text, ["DONE", "ESCALATE", "BLOCKED"])
                if v2 != "DONE":
                    raise Halt("judge sendback", "JUDGE",
                               f"fasttrack retry {n} returned {v2}: {rest2[:400]}",
                               "code wrong")
                W.commit_all(ft_wt, "g2 fasttrack: judge sendback fix")
                ft_br = s.state["chains"]["fasttrack"]["branch"]
                R.git(wt, "merge", "--no-edit", ft_br)
            else:
                if not ids:
                    ids = ["(unattributed)"]
                over = [pid for pid in ids if pid != "(unattributed)" and
                        s.get_counter(f"reexec:{pid}") >= self.cfg["caps"]["reexec_per_prompt"]]
                if over:
                    raise Halt("re-exec cap", "JUDGE",
                               f"prompts {over} exhausted re-exec cap; final judge "
                               f"defect: {rest[:600]}", "code wrong",
                               where_to_look=[str(self.run_dir / "result.diff")])
                self._reexec(ids, rest)
            # re-verify after the fix
            result2 = run_ladder(wt, self.ladder,
                                 out_json=self.run_dir / "integration-checks.json")
            baseline = json.loads(self.read_artifact("baseline-checks.json") or "{}")
            delta_d = {"hard_failure": False, "note": "baseline skipped"} \
                if baseline.get("skipped") else compute_delta(baseline, result2)
            (self.run_dir / "delta.json").write_text(json.dumps(delta_d, indent=2))
            if delta_d.get("hard_failure"):
                raise Halt("baseline delta", "JUDGE",
                           f"sendback fix introduced regressions: "
                           f"{delta_d.get('regression', []) + delta_d.get('new_failing', [])}",
                           "code wrong")
            diff = R.git(self.repo, "diff", f"{self.sha}..{result_br}", check=False)
            (self.run_dir / "result.diff").write_text(diff, encoding="utf-8")
            checks = self.read_artifact("integration-checks.json")
            delta = self.read_artifact("delta.json")
            s.append("attempts", f"judge REJECT {ids} → re-exec; verdict detail: {rest[:300]}")

    def _reexec(self, prompt_ids: list[str], hint: str) -> None:
        """REJECT → re-exec named prompts, twice at most each (§4.9)."""
        s = self.state
        plan = Plan.load(self.run_dir / "plan")
        result_br = f"g2/{s.state['run_id']}/result"
        wt = self.run_dir / "worktrees" / "result"
        for pid in prompt_ids:
            p = plan.prompt(pid)
            s.counter(f"reexec:{pid}")
            if p is None:
                continue
            chain = plan.chain(p.chain)
            prefix = self._executor_prefix(plan, chain)
            dec_values = {did: s.state["decisions"].get(did, {}).get("actual_value")
                          or (plan.decisions[did].assumed_value if did in plan.decisions else "")
                          for did in p.assumes}
            dispatch(s, self.cfg, name=f"reexec-{pid}-{s.get_counter(f'reexec:{pid}')}",
                     stage="JUDGE", tier="cheap", workdir=wt,
                     prompt=P.executor(prefix, p.body, p.id, p.title, p.acceptance,
                                       dec_values, p.records, judge_hint=hint))
            W.commit_all(wt, f"g2 re-exec {pid}: judge sendback")

    # ------------------------------------------------------------------
    # 10 · APPLY  /  HALT

    def stage_apply(self) -> dict:
        import sys
        from .metrics import finalize_metrics
        s = self.state
        W.clean_tool_noise(self.repo)
        s.set(phase="APPLY")
        s.set(terminal="DONE")
        s.emit("stage_completed", {"stage": "APPLY"})
        report = render_apply(self)
        (self.run_dir / "report.md").write_text(report, encoding="utf-8")
        finalize_metrics(s)
        print(report, file=sys.stderr)  # stdout stays JSON-clean for the eval harness
        return self._summary("DONE")

    def stage_halt(self, h: Halt) -> dict:
        """A halt is a result, not a crash. Preserve everything — branch,
        worktrees, and partial work stay on disk. rollback remains manual."""
        import sys
        from .metrics import finalize_metrics
        s = self.state
        if s.state.get("repo_path"):
            W.clean_tool_noise(s.state["repo_path"])
        s.set(terminal="HALT", phase="HALT")
        s.emit("halt", {"gate": h.gate, "stage": h.stage, "stuck": h.stuck,
                        "category": h.category})
        report = render_halt(self, h)
        (self.run_dir / "report.md").write_text(report, encoding="utf-8")
        finalize_metrics(s)
        print(report, file=sys.stderr)  # stdout stays JSON-clean for the eval harness
        summary = self._summary("HALT")
        summary["halt"] = {"gate": h.gate, "stage": h.stage, "category": h.category,
                           "stuck": h.stuck}
        return summary
