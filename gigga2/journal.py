"""Run state (master plan §3).

Append-only `journal.jsonl` is authoritative; `state.json` is a derived cache.
Deleting `state.json` must reconstruct identical state by replay.
The sequence counter is cached — the journal is never rescanned per append.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class RunState:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.run_dir / "journal.jsonl"
        self.state_path = self.run_dir / "state.json"
        self._lock = threading.Lock()
        self.state: dict = {
            "run_id": None,
            "phase": "INTAKE",
            "terminal": None,           # DONE | HALT
            "repo_path": None,
            "baseline_sha": None,
            "baseline_branch": None,
            "dirty": None,
            "request": None,
            "path": None,               # fasttrack | discovery | full
            "counters": {},             # loop caps: kickbacks, replans per chain, ...
            "chains": {},               # chain_id -> {status, worktree, branch, prompts_done}
            "branches": [],             # every branch created (for rollback)
            "worktrees": [],            # every worktree created (for rollback)
            "decisions": {},            # decision id -> {actual_value, decided_by, recorded}
            "assumptions": [],          # [ASSUMPTION] defaults applied
            "verdicts": [],             # agent verdict history (attack, review, judge)
            "attempts": [],             # sendback attempt descriptions for HALT report
            "timing": {},               # stage -> {machine_s, human_s}
            "tokens": {},               # stage -> {input, output, reasoning, cache_read}
            "events": 0,
        }
        self._seq = 0
        if self.journal_path.exists():
            self._replay()

    # ---- journal core -------------------------------------------------

    def _replay(self) -> None:
        with self.journal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                self._seq = max(self._seq, ev.get("seq", 0))
                self._apply(ev)
        self._persist()

    def _apply(self, ev: dict) -> None:
        """Fold an event into derived state. Extend keys generically."""
        kind = ev.get("event")
        data = ev.get("data", {})
        if kind == "set":
            for k, v in data.items():
                self.state[k] = v
        elif kind == "counter":
            name = data["name"]
            self.state["counters"][name] = self.state["counters"].get(name, 0) + data.get("by", 1)
        elif kind == "chain":
            cid = data["id"]
            self.state["chains"].setdefault(cid, {}).update(data.get("update", {}))
        elif kind == "decision":
            self.state["decisions"][data["id"]] = data.get("update", {})
        elif kind == "append":
            self.state.setdefault(data["key"], []).append(data["value"])
        elif kind == "timing":
            stage = data["stage"]
            t = self.state["timing"].setdefault(stage, {"machine_s": 0.0, "human_s": 0.0})
            t["machine_s"] += data.get("machine_s", 0.0)
            t["human_s"] += data.get("human_s", 0.0)
        elif kind == "tokens":
            stage = data["stage"]
            tk = self.state["tokens"].setdefault(
                stage, {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0})
            for k in tk:
                tk[k] += data.get(k, 0)
        self.state["events"] = self.state.get("events", 0) + 1

    def emit(self, event: str, data: dict | None = None, **fields) -> dict:
        """Append one event to the journal and fold it into state."""
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "t": round(time.time(), 3),
                "event": event,
                "data": data or {},
            }
            ev.update(fields)
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=str) + "\n")
            self._apply(ev)
            self._persist()
            return ev

    def _persist(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- convenience ---------------------------------------------------

    def set(self, **kv) -> None:
        self.emit("set", kv)

    def counter(self, name: str, by: int = 1) -> int:
        self.emit("counter", {"name": name, "by": by})
        return self.state["counters"].get(name, 0)

    def get_counter(self, name: str) -> int:
        return self.state["counters"].get(name, 0)

    def append(self, key: str, value) -> None:
        self.emit("append", {"key": key, "value": value})

    def add_timing(self, stage: str, machine_s: float = 0.0, human_s: float = 0.0) -> None:
        self.emit("timing", {"stage": stage, "machine_s": machine_s, "human_s": human_s})

    def add_tokens(self, stage: str, usage: dict) -> None:
        if usage:
            self.emit("tokens", {"stage": stage, **usage})

    def read_events(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        out = []
        with self.journal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
