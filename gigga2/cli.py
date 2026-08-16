"""gigga2 command line.

    gigga2 start --repo <path> --request "..." | --request-file <f> [options]
    gigga2 resume --dir <run_dir>
    gigga2 status --dir <run_dir>
    gigga2 timeline --dir <run_dir>
    gigga2 rollback --dir <run_dir>
    gigga2 validate-plan --dir <run_dir>
    gigga2 rates

`start` runs the pipeline to a terminal state (DONE | HALT) and prints a JSON
summary on stdout (human-readable reports go to stderr and <run>/report.md).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import GIGGA_HOME, load_config
from .journal import RunState
from .metrics import rates, render_timeline


def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _default_run_dir(repo: str) -> Path:
    """Run dirs (and their worktrees) must live OUTSIDE the target repo —
    a worktree inside the repo dirties its git status and breaks rollback."""
    from .config import repo_id
    return GIGGA_HOME / "runs" / f"{repo_id(Path(repo))}-{_new_run_id()}"


def cmd_start(args) -> int:
    from .stages import Pipeline

    request = args.request
    if args.request_file:
        request = Path(args.request_file).read_text(encoding="utf-8")
    if not request or not request.strip():
        print("error: --request or --request-file required", file=sys.stderr)
        return 2

    run_dir = Path(args.dir) if args.dir else _default_run_dir(args.repo)
    state = RunState(run_dir)
    if not state.state.get("run_id"):
        state.set(run_id=args.run_id or _new_run_id())
    cfg = load_config()
    pipe = Pipeline(state, cfg, repo_path=args.repo, request=request,
                    allow_dirty=args.allow_dirty, no_cache=args.no_cache,
                    skip_baseline=args.skip_baseline, checks_file=args.checks,
                    non_interactive=args.non_interactive,
                    answers_file=args.answers_file,
                    force_route=None if args.route == "auto" else args.route)
    summary = pipe.run()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def cmd_resume(args) -> int:
    from .stages import Pipeline

    state = RunState(Path(args.dir))
    if not state.state.get("run_id"):
        print("error: no run found in that directory", file=sys.stderr)
        return 2
    if state.state.get("terminal"):
        if not args.force or state.state["terminal"] != "HALT":
            print(json.dumps({"ok": state.state["terminal"] == "DONE",
                              "phase": state.state["terminal"],
                              "note": "run already terminal"}))
            return 0
        # --force: un-halt and re-enter the stage that halted
        halt = next((ev["data"] for ev in reversed(state.read_events())
                     if ev["event"] == "halt"), {})
        stage = halt.get("stage") or "TRIAGE"
        # flags marking completed stages stay; the halted stage re-runs
        phase_map = {"FASTTRACK": "FASTTRACK", "RECON": "RECON",
                     "COVERAGE_CHECK": "COVERAGE_CHECK", "DISCOVERY": "DISCOVERY",
                     "CLARIFY": "CLARIFY", "PROMPT_GEN": "PROMPT_GEN",
                     "REVIEW": "REVIEW", "EXECUTE": "EXECUTE",
                     "INTEGRATE": "INTEGRATE", "JUDGE": "JUDGE"}
        state.set(terminal=None, phase=phase_map.get(stage, "TRIAGE"))
        state.emit("resume_forced", {"from_halt": halt})
    cfg = load_config()
    pipe = Pipeline(state, cfg, non_interactive=args.non_interactive,
                    answers_file=args.answers_file)
    summary = pipe.run()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def cmd_status(args) -> int:
    state = RunState(Path(args.dir))
    s = state.state
    print(json.dumps({
        "run_id": s.get("run_id"), "phase": s.get("phase"),
        "terminal": s.get("terminal"), "path": s.get("path"),
        "repo_path": s.get("repo_path"), "baseline_sha": s.get("baseline_sha"),
        "chains": s.get("chains"), "counters": s.get("counters"),
        "assumptions": s.get("assumptions"), "branches": s.get("branches"),
    }, indent=2, default=str))
    return 0


def cmd_timeline(args) -> int:
    print(render_timeline(RunState(Path(args.dir))))
    return 0


def cmd_rollback(args) -> int:
    from .worktrees import rollback

    state = RunState(Path(args.dir))
    if not state.state.get("repo_path"):
        print("error: no run found in that directory", file=sys.stderr)
        return 2
    print(json.dumps(rollback(state), indent=2))
    return 0


def cmd_validate_plan(args) -> int:
    from .planmodel import Plan, validate_plan

    run_dir = Path(args.dir)
    state = RunState(run_dir)
    plan = Plan.load(run_dir / "plan")
    errors = validate_plan(plan, state.state.get("repo_path"),
                           state.state.get("baseline_sha"))
    if errors:
        for e in errors:
            print(f"FAIL {e}")
        return 1
    print(f"OK — {len(plan.chains)} chains, {len(plan.all_prompts())} prompts, "
          f"{len(plan.decisions)} decisions")
    return 0


def cmd_rates(args) -> int:
    print(json.dumps(rates(), indent=2))
    return 0


def cmd_install(args) -> int:
    from .installer import install
    print(json.dumps(install(), indent=2))
    return 0


def cmd_uninstall(args) -> int:
    from .installer import uninstall
    print(json.dumps(uninstall(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gigga2", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="run the pipeline to DONE or HALT")
    p.add_argument("--repo", required=True)
    p.add_argument("--request")
    p.add_argument("--request-file")
    p.add_argument("--dir", help="run directory (default: ~/.gigga2/runs/<repo>-<ts>)")
    p.add_argument("--run-id")
    p.add_argument("--allow-dirty", action="store_true",
                   help="override the dirty-tree refusal (unsound)")
    p.add_argument("--no-cache", action="store_true", help="force a cold run")
    p.add_argument("--skip-baseline", action="store_true",
                   help="do not run the baseline check ladder")
    p.add_argument("--checks", help="override check-ladder detection: file of name|kind|cmd")
    p.add_argument("--non-interactive", action="store_true",
                   help="default all blocking clarify questions ([ASSUMPTION]-tagged)")
    p.add_argument("--answers-file", help="pre-written answers to clarify questions")
    p.add_argument("--route", choices=["auto", "fasttrack", "full"], default="auto",
                   help="override triage (default: auto)")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("resume", help="resume an interrupted run")
    p.add_argument("--dir", required=True)
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--answers-file")
    p.add_argument("--force", action="store_true",
                   help="un-halt a HALTed run and re-enter the stage that halted")
    p.set_defaults(fn=cmd_resume)

    for name, fn, helptext in [
            ("status", cmd_status, "run state summary"),
            ("timeline", cmd_timeline, "journal as one line per event with elapsed offsets"),
            ("rollback", cmd_rollback, "remove every branch and worktree the run created"),
            ("validate-plan", cmd_validate_plan, "computer-run plan validators"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--dir", required=True)
        p.set_defaults(fn=fn)

    p = sub.add_parser("rates", help="cross-run instrumentation rates (§9)")
    p.set_defaults(fn=cmd_rates)

    p = sub.add_parser("install", help="register the red GIGGA primary agent + sidebar flowchart plugin in opencode")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="remove the opencode agent + sidebar plugin")
    p.set_defaults(fn=cmd_uninstall)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
