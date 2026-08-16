"""Agent dispatch (master plan §6, §8).

Only the computer blocks. LLM stages produce verdicts; the runner enforces
them. No agent's self-reported exit code is trusted anywhere — verdicts are
parsed from reply text and the runner acts on them.

Dispatch is via the opencode CLI: `opencode run --dir <workdir> --format json
[-m provider/model] -f <prompt-file> <message>`. The full prompt goes in a
file (attached with -f) so huge prompt bodies never hit command-line limits.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .journal import RunState

READ_ONLY_NOTE = (
    "You are READ-ONLY for this task: never create, edit, move, or delete any file, "
    "never install anything, never run migrations or project build steps. "
    "Inspection commands only (git log/show/diff/ls-files, cat, ls, find, grep, wc, "
    "package-manager info queries, test-runner list commands)."
)


@dataclass
class AgentResult:
    name: str
    text: str = ""
    tokens: dict = field(default_factory=lambda: {
        "input": 0, "output": 0, "reasoning": 0, "cache_read": 0})
    cost: float = 0.0
    wall_s: float = 0.0
    stalled: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    error: str | None = None


def _model_args(tier: str, cfg: dict) -> list[str]:
    model = cfg.get("models", {}).get(tier)
    return ["-m", model] if model else []


TRANSIENT_MARKERS = ("database is locked", "unexpected error", "econnreset",
                     "etimedout", "rate limit", "overloaded", "429", "500", "502", "503")


def dispatch(state: RunState, cfg: dict, *, name: str, stage: str, tier: str,
             workdir: str | Path, prompt: str, extra_files: list[str] | None = None,
             wall_s: int | None = None, stall_s: int | None = None) -> AgentResult:
    """Dispatch with retry on transient runtime failures (never on verdicts)."""
    attempts = int(cfg.get("dispatch_retries", 2)) + 1
    result = AgentResult(name=name, error="never dispatched")
    for attempt in range(attempts):
        result = _dispatch_once(state, cfg, name=f"{name}" if attempt == 0 else f"{name}-r{attempt}",
                                stage=stage, tier=tier, workdir=workdir, prompt=prompt,
                                extra_files=extra_files, wall_s=wall_s, stall_s=stall_s)
        if result.text or result.timed_out or result.stalled:
            return result
        blob = (result.error or "").lower()
        if attempt < attempts - 1 and any(m in blob for m in TRANSIENT_MARKERS):
            state.emit("dispatch_retry", {"name": name, "attempt": attempt,
                                          "error": (result.error or "")[:200]})
            time.sleep(5 * (attempt + 1))
            continue
        return result
    return result


def _dispatch_once(state: RunState, cfg: dict, *, name: str, stage: str, tier: str,
                   workdir: str | Path, prompt: str, extra_files: list[str] | None = None,
                   wall_s: int | None = None, stall_s: int | None = None) -> AgentResult:
    """Dispatch one agent. Prompt text is written to run_dir/dispatch/<name>.md
    and attached with -f; the message itself is a short pointer."""
    t0 = time.time()
    wall_s = wall_s or cfg.get("max_wall_seconds", 1800)
    if stall_s is None:
        # opencode does not stream partial generations: a long single response
        # (e.g. a body writer emitting a large JSON file) produces NO output
        # events while generating. Chain executors keep the spec's 300s
        # heartbeat (§4.7); planning/review agents get a wider window.
        stall_s = cfg.get("stall_seconds", 300) if stage == "EXECUTE" \
            else cfg.get("agent_stall_seconds", 900)

    dispatch_dir = state.run_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = dispatch_dir / f"{name}.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    message = (
        f"Your complete instructions are in the attached file {prompt_path.name}. "
        "Read it in full and follow it exactly. Your final message must begin with "
        "the verdict line the instructions specify."
    )

    # NB: opencode's -f is a yargs array and greedily swallows trailing
    # positionals — the message must come FIRST, flags after.
    cmd = [
        cfg.get("runner", "opencode"), "run",
        message,
        "--dir", str(workdir),
        "--format", "json",
        "--dangerously-skip-permissions",
        "--title", f"g2:{state.state.get('run_id')}:{name}",
        "-f", str(prompt_path),
        *_model_args(tier, cfg),
    ]
    for f in extra_files or []:
        cmd += ["-f", f]

    state.emit("agent_dispatched", {"name": name, "stage": stage, "tier": tier,
                                    "workdir": str(workdir), "cmd": " ".join(cmd[:6]) + " ..."})

    result = AgentResult(name=name)
    log_path = dispatch_dir / f"{name}.jsonl"
    text_parts: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", cwd=str(workdir),
        )
    except OSError as e:
        result.error = f"failed to launch {cmd[0]}: {e}"
        state.emit("agent_error", {"name": name, "error": result.error})
        return result

    last_output = time.time()
    done = threading.Event()

    def _reader() -> None:
        with log_path.open("w", encoding="utf-8") as log:
            for line in proc.stdout:  # type: ignore[union-attr]
                last = time.time()
                nonlocal_last[0] = last
                log.write(line)
                log.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                part = ev.get("part", {})
                if etype == "text" and part.get("text"):
                    text_parts.append(part["text"])
                elif etype == "step_finish":
                    tk = part.get("tokens", {}) or {}
                    result.tokens["input"] += tk.get("input", 0)
                    result.tokens["output"] += tk.get("output", 0)
                    result.tokens["reasoning"] += tk.get("reasoning", 0)
                    result.tokens["cache_read"] += (tk.get("cache", {}) or {}).get("read", 0)
                    result.cost += part.get("cost", 0.0) or 0.0
        done.set()

    nonlocal_last = [time.time()]
    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    while not done.is_set():
        time.sleep(1)
        now = time.time()
        if now - t0 > wall_s:
            result.timed_out = True
            break
        if now - nonlocal_last[0] > stall_s:
            result.stalled = True
            break
    if result.timed_out or result.stalled:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    done.set()
    reader.join(timeout=5)

    result.exit_code = proc.returncode
    result.wall_s = round(time.time() - t0, 1)
    result.text = "\n".join(text_parts).strip()
    if not result.text:
        # fall back to the raw log tail so failures are diagnosable
        try:
            raw = log_path.read_text(encoding="utf-8", errors="replace")
            result.error = f"no text output; log tail: {raw[-1500:]}"
        except OSError:
            result.error = "no text output"

    state.add_timing(stage, machine_s=result.wall_s)
    state.add_tokens(stage, result.tokens)
    state.emit("agent_finished", {
        "name": name, "stage": stage, "tier": tier, "wall_s": result.wall_s,
        "tokens": result.tokens, "cost": result.cost,
        "stalled": result.stalled, "timed_out": result.timed_out,
        "exit_code": result.exit_code, "error": result.error,
    })
    return result


def dispatch_parallel(state: RunState, cfg: dict, jobs: list[dict],
                      max_workers: int | None = None) -> list[AgentResult]:
    """Run independent dispatches concurrently (master plan §5.4)."""
    if not jobs:
        return []
    if len(jobs) == 1:
        return [dispatch(state, cfg, **jobs[0])]
    with ThreadPoolExecutor(max_workers=max_workers or len(jobs)) as ex:
        futs = [ex.submit(dispatch, state, cfg, **job) for job in jobs]
        return [f.result() for f in futs]


def parse_verdict(text: str, allowed: list[str]) -> tuple[str | None, str]:
    """Extract a verdict from an agent reply.

    Looks for a line starting with one of `allowed` (exact prefix match),
    scanning from the end so the final verdict wins over quoted examples.
    Returns (verdict, remainder-of-reply-from-that-line).
    """
    if not text:
        return None, ""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip().lstrip("#* ").strip()
        for v in allowed:
            if stripped == v or stripped.startswith(v + ":") or stripped.startswith(v + " "):
                return v, "\n".join(lines[i:]).strip()
    return None, text


def parse_records(text: str) -> dict[str, str]:
    """Parse executor decision records:

        DONE
        records:
          <decision-id>: <actual_value>
    """
    records: dict[str, str] = {}
    lines = text.splitlines()
    in_records = False
    for ln in lines:
        s = ln.strip()
        if s.lower() == "records:":
            in_records = True
            continue
        if in_records:
            if not s:
                break
            if ":" in s:
                k, _, v = s.partition(":")
                records[k.strip()] = v.strip()
    return records
