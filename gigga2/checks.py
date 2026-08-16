"""GIGGA v2 check ladder.

Implements:
- detect_ladder:  discover typecheck/lint/unit/e2e checks for a repo
- run_ladder:     execute checks, capture per-test ids (junit) or per-command
                  pass/fail, never crash on missing tools (recorded as skipped)
- compute_delta:  classify baseline -> current per-test changes
- select_impacted_tests: BFS over reversed import graph from changed files

Python 3.9+, stdlib only. Works on Windows and POSIX.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

KIND_ORDER = {"typecheck": 0, "lint": 1, "unit": 2, "e2e": 3}
VALID_KINDS = frozenset(KIND_ORDER)

CONFIG_BASENAMES = frozenset({
    "package.json", "package-lock.json", "package-lock",
    "pyproject.toml", "pytest.ini", "setup.cfg",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
})


@dataclass
class Check:
    name: str          # e.g. "pytest", "tsc", "eslint"
    kind: str          # "typecheck" | "lint" | "unit" | "e2e"
    cmd: str           # shell command, run from repo root
    id_mode: str = "command"   # "junit" | "command"


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _dedupe_and_order(checks: list[Check]) -> list[Check]:
    seen = set()
    out = []
    for c in checks:
        key = (c.kind, c.cmd)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    out.sort(key=lambda c: KIND_ORDER.get(c.kind, 99))
    return out


def _detect_override(override_file: Path) -> list[Check]:
    checks = []
    for line in _read_text(Path(override_file)).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            name, kind, cmd = parts[0], parts[1], "|".join(parts[2:])
        elif len(parts) == 2:
            name, kind, cmd = parts[0], "unit", parts[1]
        else:
            continue
        if not name or not cmd:
            continue
        if kind not in VALID_KINDS:
            kind = "unit"
        checks.append(Check(name=name, kind=kind, cmd=cmd))
    return checks


def _detect_node(repo: Path) -> list[Check]:
    checks = []
    pkg_file = repo / "package.json"
    if not pkg_file.is_file():
        return checks
    try:
        pkg = json.loads(_read_text(pkg_file))
    except json.JSONDecodeError:
        return checks
    scripts = pkg.get("scripts") or {}
    dev = pkg.get("devDependencies") or {}

    for script in ("typecheck", "type-check", "tsc"):
        if script in scripts:
            checks.append(Check(script, "typecheck", f"npm run {script}"))
            break
    else:
        if (repo / "tsconfig.json").is_file() and "typescript" in dev:
            checks.append(Check("tsc", "typecheck", "npx tsc --noEmit"))

    if "lint" in scripts:
        checks.append(Check("lint", "lint", "npm run lint"))

    if "test" in scripts:
        if "jest" in dev:
            checks.append(Check("jest", "unit", "npx jest --ci"))
        elif "vitest" in dev:
            checks.append(Check("vitest", "unit", "npx vitest run"))
        else:
            checks.append(Check("npm-test", "unit", "npm test"))

    e2e_script = next(
        (s for s in scripts if s == "e2e" or s.startswith("test:e2e")), None
    )
    if e2e_script:
        checks.append(Check(e2e_script, "e2e", f"npm run {e2e_script}"))
    elif any(repo.glob("playwright.config.*")):
        checks.append(Check("playwright", "e2e", "npx playwright test"))
    return checks


def _detect_python(repo: Path) -> list[Check]:
    checks = []
    pyproject = repo / "pyproject.toml"
    pyproject_text = _read_text(pyproject) if pyproject.is_file() else ""

    has_pytest_cfg = (
        pyproject.is_file()
        or (repo / "pytest.ini").is_file()
        or (repo / "tox.ini").is_file()
        or ((repo / "setup.cfg").is_file()
            and "[tool:pytest]" in _read_text(repo / "setup.cfg"))
    )
    if has_pytest_cfg:
        checks.append(Check("pytest", "unit",
                            "python -m pytest -q -p no:cacheprovider",
                            id_mode="junit"))

    if (repo / "mypy.ini").is_file() or "[tool.mypy]" in pyproject_text:
        checks.append(Check("mypy", "typecheck", "python -m mypy ."))

    if ("[tool.ruff]" in pyproject_text
            or (repo / "ruff.toml").is_file()
            or (repo / ".ruff.toml").is_file()):
        checks.append(Check("ruff", "lint", "python -m ruff check ."))
    return checks


def detect_ladder(repo_path: Path, override_file: Path | None = None) -> list[Check]:
    """Return the ordered check ladder (typecheck, lint, unit, e2e)."""
    repo = Path(repo_path)
    if override_file is not None:
        return _dedupe_and_order(_detect_override(Path(override_file)))

    checks: list[Check] = []
    checks.extend(_detect_node(repo))
    checks.extend(_detect_python(repo))

    if (repo / "go.mod").is_file():
        checks.append(Check("go vet", "typecheck", "go vet ./..."))
        checks.append(Check("go test", "unit", "go test ./..."))

    if (repo / "Cargo.toml").is_file():
        checks.append(Check("cargo check", "typecheck", "cargo check"))
        # clippy intentionally skipped (too slow/flaky)
        checks.append(Check("cargo test", "unit", "cargo test"))

    return _dedupe_and_order(checks)


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def _parse_junit(path: str) -> dict:
    """Parse junit xml -> {classname::name: 'pass'|'fail'}. {} on failure."""
    results: dict = {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return results
    for tc in root.iter("testcase"):
        classname = tc.get("classname") or ""
        name = tc.get("name") or ""
        test_id = f"{classname}::{name}" if classname else name
        if not test_id:
            continue
        failed = tc.find("failure") is not None or tc.find("error") is not None
        results[test_id] = "fail" if failed else "pass"
    return results


def _tail(output: str, n: int = 40) -> str:
    return "\n".join(output.splitlines()[-n:])


def run_ladder(repo_path: Path, ladder: list[Check], out_json: Path | None = None,
               kinds: set[str] | None = None, only_tests: list[str] | None = None,
               timeout_s: int = 1800) -> dict:
    """Run the ladder; capture per-check and per-test results.

    A check whose tool is absent from PATH is recorded as skipped, never
    raises. ``only_tests`` (impact mode) restricts pytest checks to the given
    test file paths.
    """
    repo = Path(repo_path)
    selected = [c for c in ladder if kinds is None or c.kind in kinds]

    check_results = []
    tests: dict = {}
    all_ok = True

    for check in selected:
        entry = {
            "name": check.name, "kind": check.kind, "cmd": check.cmd,
            "exit": None, "duration_s": 0.0, "skipped": False, "tail": "",
        }
        check_results.append(entry)

        tokens = check.cmd.split()
        tool = tokens[0] if tokens else ""
        if tool and shutil.which(tool) is None:
            entry["skipped"] = True
            entry["tail"] = f"skipped: tool '{tool}' not found on PATH"
            continue

        cmd = check.cmd
        is_pytest = "pytest" in check.cmd
        if only_tests is not None and is_pytest:
            if not only_tests:
                entry["skipped"] = True
                entry["tail"] = "skipped: impact mode selected no tests"
                continue
            cmd += " " + " ".join(f'"{t}"' for t in only_tests)

        xml_path = None
        if check.id_mode == "junit":
            fd, xml_path = tempfile.mkstemp(prefix="gigga-junit-", suffix=".xml")
            os.close(fd)
            cmd += f' --junitxml="{xml_path}"'

        start = time.monotonic()
        try:
            env = dict(os.environ)
            # keep the user's tree pristine: no __pycache__ / .pytest_cache
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                cmd, shell=True, cwd=str(repo), timeout=timeout_s, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
            )
            exit_code = proc.returncode
            output = proc.stdout or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            output = (out or "") + f"\n<timed out after {timeout_s}s>"
        except OSError as exc:
            exit_code = -1
            output = f"<failed to launch: {exc}>"
        entry["duration_s"] = round(time.monotonic() - start, 3)
        entry["exit"] = exit_code
        entry["tail"] = _tail(output)

        if exit_code != 0:
            all_ok = False

        if check.id_mode == "junit":
            if xml_path is not None:
                tests.update(_parse_junit(xml_path))
                try:
                    os.unlink(xml_path)
                except OSError:
                    pass
        else:
            tests[check.cmd] = "pass" if exit_code == 0 else "fail"

    result = {
        "ok": all_ok,
        "mode": "impact" if only_tests is not None else "full",
        "checks": check_results,
        "tests": tests,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "empty": len(selected) == 0,
    }
    if out_json is not None:
        out_json = Path(out_json)
        if out_json.parent != out_json:
            out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------- #
# delta
# --------------------------------------------------------------------------- #

def compute_delta(baseline: dict, current: dict) -> dict:
    """Classify per-test changes between two run_ladder-shaped dicts."""
    base = baseline.get("tests") or {}
    cur = current.get("tests") or {}

    regression = sorted(k for k in base
                        if base[k] == "pass" and cur.get(k) == "fail")
    new_failing = sorted(k for k in cur
                         if cur[k] == "fail" and k not in base)
    fix = sorted(k for k in base
                 if base[k] == "fail" and cur.get(k) == "pass")
    new_passing = sorted(k for k in cur
                         if cur[k] == "pass" and k not in base)
    still_failing = sorted(k for k in base
                           if base[k] == "fail" and cur.get(k) == "fail")

    return {
        "regression": regression,
        "new_failing": new_failing,
        "fix": fix,
        "new_passing": new_passing,
        "still_failing": still_failing,
        "hard_failure": bool(regression or new_failing),
    }


# --------------------------------------------------------------------------- #
# impact selection
# --------------------------------------------------------------------------- #

def _norm_rel(path: str) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _looks_like_config(rel: str) -> bool:
    rel = _norm_rel(rel)
    base = rel.rsplit("/", 1)[-1]
    if rel.startswith(".github/"):
        return True
    if base in CONFIG_BASENAMES:
        return True
    if fnmatch(base, "tsconfig*.json"):
        return True
    if fnmatch(base, "*.config.js") or fnmatch(base, "*.config.ts"):
        return True
    return False


def select_impacted_tests(probe: dict, changed_files: list[str]) -> tuple[list[str] | None, str]:
    """Pick test files affected by ``changed_files`` via the import graph.

    Returns (None, reason) when the caller must fall back to the full suite,
    (sorted_tests, message) otherwise.
    """
    edges = probe.get("import_edges") or []
    test_files = {_norm_rel(t) for t in (probe.get("test_files") or [])}

    if not edges:
        return None, "impact: probe has no import edges; falling back to full suite"

    for f in changed_files:
        if _looks_like_config(f):
            return None, (f"impact: changed file '{f}' is a config/manifest; "
                          "falling back to full suite")

    reverse: dict = {}
    for edge in edges:
        try:
            frm, to = edge[0], edge[1]
        except (TypeError, IndexError):
            continue
        reverse.setdefault(_norm_rel(to), []).append(_norm_rel(frm))

    impacted: set = set()
    seen: set = set()
    stack = [_norm_rel(f) for f in changed_files]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in test_files:
            impacted.add(cur)
        stack.extend(reverse.get(cur, []))

    if not impacted:
        return [], "impact: no tests reach changed files"
    return sorted(impacted), f"impact: {len(impacted)} of {len(test_files)} test files selected"
