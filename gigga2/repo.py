"""Repo binding (master plan §4.0 INTAKE) and small git helpers.

Everything downstream is anchored to `baseline_sha`; a dirty tree makes a run unsound.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, check: bool = True, timeout: int = 120) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and out.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({out.returncode}): {out.stderr.strip()}")
    return out.stdout


def bind_repo(repo_path: str | Path, allow_dirty: bool = False) -> dict:
    """Resolve and validate the repo. Refuse a dirty working tree unless overridden."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise GitError(f"not a git repository: {repo}")
    sha = git(repo, "rev-parse", "HEAD").strip()
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = bool(git(repo, "status", "--porcelain").strip())
    if dirty and not allow_dirty:
        raise GitError(
            "working tree is dirty — commit or stash first "
            "(or pass --allow-dirty; a dirty tree makes the run unsound)"
        )
    return {
        "repo_path": str(repo),
        "baseline_sha": sha,
        "baseline_branch": branch,
        "dirty": dirty,
    }


def file_at(repo: Path | str, sha: str, path: str) -> str | None:
    """Content of `path` at `sha`; None if absent."""
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"],
        capture_output=True, timeout=60,
    )
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def resolve_cite(repo: Path | str, sha: str, anchor: str) -> str | None:
    """Resolve a cite anchor 'path' or 'path:start-end' at sha. None if unresolvable."""
    if ":" in anchor:
        path, _, span = anchor.rpartition(":")
        content = file_at(repo, sha, path)
        if content is None:
            return None
        lines = content.splitlines()
        try:
            start, _, end = span.partition("-")
            s, e = int(start), int(end or start)
        except ValueError:
            return None
        if s < 1 or s > len(lines):
            return None
        e = min(e, len(lines))
        return "\n".join(lines[s - 1:e])
    return file_at(repo, sha, anchor)


def cite_hash_in_tree(tree_path: Path | str, anchor: str) -> str | None:
    """Hash a cite anchor against a worktree's current state (pre-flight, §4.7a)."""
    import hashlib

    tree = Path(tree_path)
    if ":" in anchor:
        path, _, span = anchor.rpartition(":")
        f = tree / path
        if not f.is_file():
            return None
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            start, _, end = span.partition("-")
            s, e = int(start), int(end or start)
        except ValueError:
            return None
        if s < 1 or s > len(lines):
            return None
        e = min(e, len(lines))
        content = "\n".join(lines[s - 1:e])
    else:
        f = tree / anchor
        if not f.is_file():
            return None
        content = f.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def hash_cite_at(repo: Path | str, sha: str, anchor: str) -> str | None:
    """Hash a cite anchor's content at a given sha (plan time)."""
    import hashlib

    content = resolve_cite(repo, sha, anchor)
    if content is None:
        return None
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def diffstat(repo: Path | str, base: str, head: str) -> str:
    return git(repo, "diff", "--stat", f"{base}..{head}", check=False).strip()


def changed_files(repo: Path | str, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", f"{base}..{head}", check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]
