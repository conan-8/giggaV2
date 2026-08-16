"""Worktree lifecycle (master plan §4.7, §4.8, §4.10).

One worktree per chain, branched from baseline_sha. Never checkout, never
touch the user's working tree. Every branch/worktree created is journalled
so `rollback` can remove exactly what the run created.
"""

from __future__ import annotations

from pathlib import Path

from .journal import RunState
from .repo import git


def branch_name(run_id: str, chain: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in chain)
    return f"g2/{run_id}/{safe}"


def add_worktree(state: RunState, repo: str, chain: str, base_ref: str) -> tuple[Path, str]:
    """git worktree add <run>/worktrees/<chain> -b g2/<run_id>/<chain> <base_ref>"""
    wt = state.run_dir / "worktrees" / chain
    wt.parent.mkdir(parents=True, exist_ok=True)
    br = branch_name(state.state["run_id"], chain)
    if (wt / ".git").exists():
        # re-entry (e.g. REJECT-PLAN loop): reuse the existing worktree/branch
        if not git(repo, "branch", "--list", br).strip():
            git(repo, "branch", br, base_ref)
    else:
        git(repo, "worktree", "add", str(wt), "-b", br, base_ref)
    state.append("worktrees", str(wt))
    state.append("branches", br)
    state.emit("chain", {"id": chain, "update": {"worktree": str(wt), "branch": br}})
    return wt, br


def remove_worktree(state: RunState, repo: str, chain: str, delete_branch: bool = False) -> None:
    info = state.state["chains"].get(chain, {})
    wt, br = info.get("worktree"), info.get("branch")
    if wt:
        git(repo, "worktree", "remove", "--force", wt, check=False)
    if delete_branch and br:
        git(repo, "branch", "-D", br, check=False)


def commit_all(repo_or_worktree: str | Path, message: str) -> str | None:
    """Commit any working tree changes; returns new sha or None if nothing to commit."""
    wt = str(repo_or_worktree)
    git(wt, "add", "-A")
    # never commit tool noise the agent's own test runs produced
    added = git(wt, "diff", "--cached", "--name-only", "--diff-filter=A", check=False)
    for line in added.splitlines():
        parts = line.split("/")
        if line.endswith(".pyc") or "__pycache__" in parts or ".pytest_cache" in parts:
            git(wt, "reset", "-q", "--", line, check=False)
    # judge by the STAGED tree, not porcelain — untracked tool noise must not
    # trick us into a "nothing to commit" failure
    if not git(wt, "diff", "--cached", "--name-only").strip():
        return None
    git(wt, "-c", "user.name=gigga2", "-c", "user.email=gigga2@localhost",
        "commit", "-m", message)
    return git(wt, "rev-parse", "HEAD").strip()


def clean_tool_noise(repo: str | Path) -> None:
    """Remove untracked tool-noise dirs (__pycache__, .pytest_cache) that
    read-only verification runs (baseline ladder, judge) leave behind."""
    git(repo, "clean", "-fd", "--", "**/__pycache__", "**/.pytest_cache", check=False)


def rollback(state: RunState) -> dict:
    """Remove every branch and worktree the run created; leave git status clean."""
    repo = state.state["repo_path"]
    removed = {"worktrees": [], "branches": [], "errors": []}
    for wt in state.state.get("worktrees", []):
        out = git(repo, "worktree", "remove", "--force", wt, check=False)
        removed["worktrees"].append(wt)
    git(repo, "worktree", "prune", check=False)
    clean_tool_noise(repo)
    for br in state.state.get("branches", []):
        try:
            git(repo, "branch", "-D", br)
            removed["branches"].append(br)
        except Exception as e:  # noqa: BLE001 - report and continue
            removed["errors"].append(f"{br}: {e}")
    clean = not git(repo, "status", "--porcelain").strip()
    removed["git_status_clean"] = clean
    state.emit("set", {"rolled_back": True})
    return removed
