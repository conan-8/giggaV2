#!/usr/bin/env python3
"""GIGGA v2 eval harness.

Usage:
    ./eval run --arm v2 --task all [--repeats 3]
    ./eval run --arm v2 --task <task_id> [--repeats 3]
    ./eval run --arm all --task all [--repeats 3]
    ./eval report [--arm <arm>] [--task <task_id>]
    ./eval list

Arms:
    v2          GIGGA v2 full pipeline
    v1          GIGGA v1 pipeline
    cheap       plain plan+build, cheap model
    strong      plain plan+build, strongest model

Tasks are defined in eval/tasks/*.json.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

EVAL_DIR = Path(__file__).parent
TASKS_DIR = EVAL_DIR / "tasks"
RESULTS_DIR = EVAL_DIR / "results"


def runner_cmd():
    """Resolve the gigga2 runner: installed CLI on PATH, else python -m gigga2."""
    if shutil.which("gigga2"):
        return ["gigga2"]
    return [sys.executable, "-m", "gigga2"]

ARMS = ["v2", "v1", "cheap", "strong"]


def load_tasks():
    tasks = {}
    for f in sorted(TASKS_DIR.glob("*.json")):
        task = json.loads(f.read_text())
        tasks[task["id"]] = task
    return tasks


def clone_repo(repo_path, dest):
    subprocess.run(["git", "clone", "--no-hardlinks", str(repo_path), str(dest)],
                   capture_output=True, check=True)
    return dest


def checkout_baseline(clone_path, baseline_sha):
    subprocess.run(["git", "-C", str(clone_path), "checkout", baseline_sha],
                   capture_output=True, check=True)


def run_arm_v2(task, clone_path, run_dir):
    req_file = run_dir / "request.txt"
    req_file.write_text(task["prompt"])

    start = time.time()
    result = subprocess.run(
        runner_cmd() + ["start",
         "--repo", str(clone_path),
         "--request-file", str(req_file),
         "--dir", str(run_dir / "state"),
         "--skip-baseline",
         "--non-interactive"],
        capture_output=True, text=True,
    )
    wall = time.time() - start

    output = {}
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        # tolerate log lines around the JSON summary: take the last {...} block
        import re
        blocks = re.findall(r"\{.*\}", result.stdout, re.S)
        if blocks:
            try:
                output = json.loads(blocks[-1])
            except json.JSONDecodeError:
                output = {"raw": result.stdout[-2000:], "stderr": result.stderr[-2000:]}
        else:
            output = {"raw": result.stdout[-2000:], "stderr": result.stderr[-2000:]}

    return {
        "wall_clock_s": round(wall, 2),
        "start_ok": output.get("ok", False),
        "state_dir": output.get("state_dir"),
        "phase": output.get("phase"),
        "warning": output.get("warning"),
    }


def run_arm_v1(task, clone_path, run_dir):
    return {"wall_clock_s": 0, "note": "v1 arm not yet wired"}


def run_arm_cheap(task, clone_path, run_dir):
    return {"wall_clock_s": 0, "note": "cheap arm not yet wired"}


def run_arm_strong(task, clone_path, run_dir):
    return {"wall_clock_s": 0, "note": "strong arm not yet wired"}


ARM_RUNNERS = {
    "v2": run_arm_v2,
    "v1": run_arm_v1,
    "cheap": run_arm_cheap,
    "strong": run_arm_strong,
}


def run_single(arm, task, repeat, repo_path):
    run_id = f"{arm}-{task['id']}-r{repeat}"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    clone_path = run_dir / "clone"
    if clone_path.exists():
        shutil.rmtree(clone_path)
    clone_repo(repo_path, clone_path)
    checkout_baseline(clone_path, task["baseline_sha"])

    runner_fn = ARM_RUNNERS[arm]
    result = runner_fn(task, clone_path, run_dir)

    result["arm"] = arm
    result["task_id"] = task["id"]
    result["repeat"] = repeat
    result["run_id"] = run_id
    result["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def cmd_run(args):
    arm = None
    task_filter = "all"
    repeats = 3

    i = 0
    while i < len(args):
        if args[i] == "--arm" and i + 1 < len(args):
            arm = args[i + 1]; i += 2
        elif args[i] == "--task" and i + 1 < len(args):
            task_filter = args[i + 1]; i += 2
        elif args[i] == "--repeats" and i + 1 < len(args):
            repeats = int(args[i + 1]); i += 2
        else:
            i += 1

    if not arm:
        print("error: --arm required (v2|v1|cheap|strong|all)")
        sys.exit(1)

    arms = ARMS if arm == "all" else [arm]
    tasks = load_tasks()

    if task_filter != "all":
        if task_filter not in tasks:
            print(f"error: unknown task '{task_filter}'")
            sys.exit(1)
        tasks = {task_filter: tasks[task_filter]}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for a in arms:
        for tid, task in tasks.items():
            repo_path = task.get("repo_path")
            if not repo_path or not Path(repo_path).exists():
                print(f"skip {a}/{tid}: repo_path not found: {repo_path}")
                continue
            for r in range(1, repeats + 1):
                print(f"running {a}/{tid} repeat {r}/{repeats}...")
                result = run_single(a, task, r, repo_path)
                results.append(result)
                print(f"  wall={result.get('wall_clock_s', '?')}s ok={result.get('start_ok', '?')}")

    summary_path = RESULTS_DIR / f"summary-{int(time.time())}.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\n{len(results)} runs complete. Summary: {summary_path}")


def cmd_report(args):
    arm_filter = None
    task_filter = None
    i = 0
    while i < len(args):
        if args[i] == "--arm" and i + 1 < len(args):
            arm_filter = args[i + 1]; i += 2
        elif args[i] == "--task" and i + 1 < len(args):
            task_filter = args[i + 1]; i += 2
        else:
            i += 1

    if not RESULTS_DIR.exists():
        print("no results yet")
        return

    results = []
    for d in sorted(RESULTS_DIR.iterdir()):
        rf = d / "result.json"
        if rf.exists():
            r = json.loads(rf.read_text())
            if arm_filter and r.get("arm") != arm_filter:
                continue
            if task_filter and r.get("task_id") != task_filter:
                continue
            results.append(r)

    if not results:
        print("no matching results")
        return

    by_arm = {}
    for r in results:
        by_arm.setdefault(r["arm"], []).append(r)

    for arm, runs in sorted(by_arm.items()):
        walls = [r.get("wall_clock_s", 0) for r in runs]
        print(f"\n{arm}: {len(runs)} runs")
        print(f"  wall_clock: min={min(walls):.1f}s max={max(walls):.1f}s mean={sum(walls)/len(walls):.1f}s")
        for r in runs:
            print(f"    {r['task_id']} r{r['repeat']}: {r.get('wall_clock_s', '?')}s ok={r.get('start_ok', '?')}")


def cmd_list(args):
    tasks = load_tasks()
    if not tasks:
        print("no tasks defined in eval/tasks/")
        return
    for tid, task in sorted(tasks.items()):
        print(f"  {tid}: {task.get('title', '?')} [{task.get('category', '?')}]")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "run":
        cmd_run(args)
    elif cmd == "report":
        cmd_report(args)
    elif cmd == "list":
        cmd_list(args)
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
