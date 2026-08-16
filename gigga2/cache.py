"""Artifact cache, keyed by baseline_sha (master plan §5.1).

Cache probe.md, findings (repo section), and baseline-checks.json under
~/.gigga2/cache/<repo-id>/<baseline_sha>/. Invalidate on any new commit —
never on a timer, never partially. --no-cache forces a cold run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import CACHE_DIR, repo_id

REPO_SECTION_MARKERS = ("<!-- repo-section -->", "<!-- /repo-section -->")
TASK_SECTION_MARKERS = ("<!-- task-section -->", "<!-- /task-section -->")


class ArtifactCache:
    def __init__(self, repo_path: Path | str, baseline_sha: str, enabled: bool = True):
        self.dir = CACHE_DIR / repo_id(Path(repo_path)) / baseline_sha
        self.enabled = enabled

    def _path(self, name: str) -> Path:
        return self.dir / name

    def has(self, name: str) -> bool:
        return self.enabled and self._path(name).is_file()

    def read(self, name: str) -> str | None:
        if not self.has(name):
            return None
        return self._path(name).read_text(encoding="utf-8")

    def read_json(self, name: str) -> dict | None:
        raw = self.read(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def write(self, name: str, content: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(content, encoding="utf-8")

    def write_json(self, name: str, data: dict) -> None:
        self.write(name, json.dumps(data, indent=2, default=str))

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "dir": str(self.dir),
            "probe.md": self.has("probe.md"),
            "probe.json": self.has("probe.json"),
            "findings-repo.md": self.has("findings-repo.md"),
            "baseline-checks.json": self.has("baseline-checks.json"),
        }


def split_findings(findings_md: str) -> tuple[str | None, str | None]:
    """Split findings.md into (repo section, task section) using HTML markers.

    Recon emits findings with the repo-cacheable part wrapped in
    <!-- repo-section --> ... <!-- /repo-section -->. Returns (None, None)
    pieces that are missing.
    """
    rs, re_ = REPO_SECTION_MARKERS
    ts, te = TASK_SECTION_MARKERS

    def _extract(start: str, end: str) -> str | None:
        i = findings_md.find(start)
        j = findings_md.find(end)
        if i == -1 or j == -1 or j <= i:
            return None
        return findings_md[i + len(start):j].strip()

    return _extract(rs, re_), _extract(ts, te)


def merge_findings(repo_section: str, task_section: str) -> str:
    rs, re_ = REPO_SECTION_MARKERS
    ts, te = TASK_SECTION_MARKERS
    return f"{rs}\n{repo_section}\n{re_}\n\n{ts}\n{task_section}\n{te}\n"


def prune_repo_cache(repo_path: Path | str, keep_sha: str) -> None:
    """Invalidate-on-commit helper: drop cache dirs for other SHAs of this repo."""
    base = CACHE_DIR / repo_id(Path(repo_path))
    if not base.is_dir():
        return
    for child in base.iterdir():
        if child.is_dir() and child.name != keep_sha:
            shutil.rmtree(child, ignore_errors=True)
