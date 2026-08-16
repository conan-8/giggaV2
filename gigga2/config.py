"""Run configuration: model tiers, paths, limits.

Model tiers (master plan §8):
    strongest — RECON, PROMPT_GEN 5a+5b, plan review, JUDGE, DISCOVERY
    strong    — FASTTRACK, gap agent, attack, merge agent
    cheap     — TRIAGE, EXECUTE

Tier → model mapping lives in ~/.gigga2/config.json. A null tier means
"use the agent runtime's default model". See docs/model-allocation.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

GIGGA_HOME = Path(os.environ.get("GIGGA2_HOME", Path.home() / ".gigga2"))
CACHE_DIR = GIGGA_HOME / "cache"
CONFIG_PATH = GIGGA_HOME / "config.json"
METRICS_LOG = GIGGA_HOME / "metrics.jsonl"

DEFAULT_CONFIG = {
    # opencode model strings ("provider/model"); null = runtime default model.
    "models": {"strongest": None, "strong": None, "cheap": None},
    # agent runtime command; must support `run --dir <d> [-m m] [-f file] --format json <msg>`
    "runner": "opencode",
    # executor bounds (master plan §4.7)
    "max_wall_seconds": 1800,
    "stall_seconds": 300,
    # non-chain agents: longer heartbeat (no streamed tokens during generation)
    "agent_stall_seconds": 900,
    # transient runtime-error retries per dispatch
    "dispatch_retries": 2,
    # granularity guardrail (master plan §4.5)
    "prompt_count_warn": 20,
    # loop caps (master plan §7)
    "caps": {
        "coverage_gap": 2,
        "fasttrack_escalate": 1,
        "fasttrack_reject": 2,
        "plan_reject": 2,      # shared between REVIEW 6a and JUDGE REJECT-PLAN
        "kickback": 3,
        "replan_per_chain": 2,
        "reexec_per_prompt": 2,
    },
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _deep_update(cfg, user)
        except (json.JSONDecodeError, OSError):
            pass
    else:
        GIGGA_HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    # env overrides: GIGGA2_MODEL_STRONGEST=anthropic/claude-... etc.
    for tier in ("strongest", "strong", "cheap"):
        env = os.environ.get(f"GIGGA2_MODEL_{tier.upper()}")
        if env:
            cfg["models"][tier] = env
    return cfg


def _deep_update(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def repo_id(repo_path: Path) -> str:
    """Stable id for the artifact cache: origin URL if any, else absolute path."""
    import hashlib
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=15,
        )
        key = out.stdout.strip() or str(repo_path.resolve())
    except (OSError, subprocess.SubprocessError):
        key = str(repo_path.resolve())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
