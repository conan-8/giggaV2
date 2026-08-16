"""GIGGA v2 structural repo probe.

Deterministic, computer-only scan of a git repository. Produces a compact
probe.md (<= ~8KB) map for LLM agents and a full probe.json with untruncated
data. Stdlib only, Python 3.9+, works on Windows (git bash) and POSIX.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", "target", ".next", "vendor",
}

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".c": "c", ".h": "c/c++ header", ".cpp": "c++",
    ".cc": "c++", ".hpp": "c++", ".cs": "c#", ".css": "css",
    ".scss": "css", ".html": "html", ".htm": "html", ".md": "markdown",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sql": "sql", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".php": "php", ".swift": "swift", ".kt": "kotlin", ".lua": "lua",
    ".xml": "xml", ".vue": "vue", ".svelte": "svelte",
}

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under", "again",
    "further", "once", "here", "there", "all", "any", "both", "each",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "will",
    "just", "should", "now", "of", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "this",
    "that", "these", "those", "it", "its", "as", "we", "you", "they",
    "he", "she", "i", "me", "my", "your", "their", "our", "what", "which",
    "who", "how", "use", "using", "used",
}

TEXT_EXTS = set(LANG_BY_EXT) | {
    ".txt", ".ini", ".cfg", ".conf", ".env", ".lock", ".mod", ".sum",
    ".gradle", ".properties", ".gitignore", ".dockerignore",
}

MD_BUDGET = 7900  # bytes, keep probe.md under ~8KB


# ---------------------------------------------------------------------------
# walking helpers
# ---------------------------------------------------------------------------

def _walk_files(root: Path):
    """Yield repo-relative POSIX paths of all non-excluded files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            yield p.relative_to(root).as_posix()


def _read_text(path: Path, cap: int) -> str | None:
    """Read a file as text; None if binary-ish, too large, or unreadable."""
    try:
        if path.stat().st_size > cap:
            return None
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            return None
        return data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _tree_lines(root: Path, max_depth: int = 3) -> list[str]:
    """Directory tree to max_depth; file counts aggregate deeper dirs."""
    lines = [root.name + "/"]

    def count_files(d: Path) -> int:
        n = 0
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDED_DIRS]
            n += len(filenames)
        return n

    def recurse(d: Path, depth: int, prefix: str):
        try:
            entries = sorted(
                [e for e in d.iterdir() if e.is_dir() and e.name not in EXCLUDED_DIRS],
                key=lambda e: e.name.lower(),
            )
        except OSError:
            return
        for i, e in enumerate(entries):
            n = count_files(e)
            lines.append(f"{prefix}{e.name}/ ({n} files)")
            if depth < max_depth:
                recurse(e, depth + 1, prefix + "  ")

    recurse(root, 1, "  ")
    return lines


def _loc_by_lang(root: Path) -> dict[str, int]:
    loc: dict[str, int] = defaultdict(int)
    for rel in _walk_files(root):
        lang = LANG_BY_EXT.get(Path(rel).suffix.lower())
        if not lang:
            continue
        text = _read_text(root / rel, cap=2 * 1024 * 1024)
        if text is None:
            continue
        loc[lang] += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return dict(sorted(loc.items(), key=lambda kv: -kv[1]))


def _toml_sections(text: str) -> dict[str, str]:
    """Crude TOML section splitter: {section_name: raw body text}."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _manifest_section(root: Path) -> str:
    out: list[str] = []
    manifests = [
        "package.json", "requirements.txt", "pyproject.toml", "go.mod",
        "Cargo.toml", "Gemfile", "composer.json", "setup.py", "setup.cfg",
    ]
    # find manifests anywhere (excluding noise dirs), root first
    found: list[Path] = []
    for rel in _walk_files(root):
        if Path(rel).name in manifests:
            found.append(Path(rel))
    found.sort(key=lambda p: (len(p.parts), p.as_posix()))

    for rel in found:
        text = _read_text(root / rel, cap=1024 * 1024)
        if text is None:
            continue
        name = rel.name
        body = ""
        try:
            if name == "package.json":
                data = json.loads(text)
                keep = {k: data.get(k) for k in
                        ("dependencies", "devDependencies", "scripts") if k in data}
                body = json.dumps(keep, indent=1) if keep else "(no deps/scripts)"
            elif name == "requirements.txt":
                body = "\n".join(
                    ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")
                )
            elif name == "pyproject.toml":
                secs = _toml_sections(text)
                parts = []
                proj = secs.get("project", "")
                if proj:
                    # keep only dependency-relevant lines of [project]
                    dep_lines, in_block = [], False
                    for ln in proj.splitlines():
                        if re.match(r"\s*(dependencies|requires-python)\s*=", ln):
                            in_block = True
                        if in_block:
                            dep_lines.append(ln)
                            # block ends at ']' or after any complete single line
                            if "]" in ln or ("[" not in ln and ln.strip().endswith(('"', "'"))):
                                in_block = False
                    parts.append("[project]\n" + "\n".join(dep_lines))
                opt = secs.get("project.optional-dependencies", "")
                if opt:
                    parts.append("[project.optional-dependencies]\n" + opt)
                flags = [t for t in ("tool.pytest", "tool.pytest.ini_options",
                                     "tool.ruff", "tool.mypy", "tool.pyright")
                         if t in secs]
                if flags:
                    parts.append("config sections present: " + ", ".join(flags))
                body = "\n".join(parts) if parts else "(no [project] deps found)"
            elif name == "go.mod":
                body = text.strip()
            elif name == "Cargo.toml":
                secs = _toml_sections(text)
                parts = [f"[{s}]\n{secs[s]}" for s in
                         ("dependencies", "dev-dependencies") if s in secs]
                body = "\n".join(parts) if parts else "(no dependencies sections)"
            elif name == "Gemfile":
                body = "\n".join(
                    ln for ln in text.splitlines()
                    if ln.strip().startswith("gem ") or ln.strip().startswith("source ")
                ) or text.strip()
            elif name == "composer.json":
                data = json.loads(text)
                keep = {k: data.get(k) for k in ("require", "require-dev") if k in data}
                body = json.dumps(keep, indent=1) if keep else "(no require)"
            else:  # setup.py / setup.cfg
                body = text.strip()[:2000]
        except (json.JSONDecodeError, ValueError):
            body = text.strip()[:2000]
        out.append(f"### {rel.as_posix()}\n{body}")
    return "\n\n".join(out) if out else "(no dependency manifests found)"


def _detect(root: Path) -> dict:
    fw, fw_ev = None, []
    runner, runner_ev = None, []
    checker, checker_ev = None, []
    linter, linter_ev = None, []

    def has(rel: str) -> bool:
        return (root / rel).is_file()

    def glob1(pattern: str) -> str | None:
        try:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    return p.relative_to(root).as_posix()
        except OSError:
            pass
        return None

    # ---- framework ----
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            deps = {}
            for k in ("dependencies", "devDependencies"):
                deps.update(data.get(k) or {})
            for name, fws in (("next", "next"), ("react", "react"),
                              ("vue", "vue"), ("express", "express"),
                              ("@angular/core", "angular"), ("svelte", "svelte"),
                              ("fastify", "fastify")):
                if name in deps and fw is None:
                    fw, fw_ev = fws, ["package.json"]
        except (json.JSONDecodeError, OSError):
            pass
    pydeps = ""
    for req in ("requirements.txt", "pyproject.toml"):
        t = _read_text(root / req, cap=1024 * 1024)
        if t:
            pydeps += t.lower() + "\n"
    if fw is None:
        for name in ("django", "flask", "fastapi", "tornado", "starlette"):
            if re.search(rf"\b{name}\b", pydeps):
                src = "requirements.txt" if has("requirements.txt") else "pyproject.toml"
                fw, fw_ev = name, [src]
                break
    if fw is None and has("Gemfile"):
        t = _read_text(root / "Gemfile", cap=1024 * 1024) or ""
        if re.search(r"gem\s+['\"]rails['\"]", t):
            fw, fw_ev = "rails", ["Gemfile"]
    if fw is None and has("manage.py"):
        fw, fw_ev = "django", ["manage.py"]

    # ---- test runner ----
    if has("pytest.ini") or "[tool.pytest" in pydeps or has("setup.cfg") and "[tool:pytest]" in (
            _read_text(root / "setup.cfg", cap=1024 * 1024) or ""):
        ev = [p for p in ("pytest.ini", "pyproject.toml", "setup.cfg") if has(p)]
        runner, runner_ev = "pytest", ev
    elif re.search(r"\bpytest\b", pydeps):
        runner, runner_ev = "pytest", ["requirements.txt" if has("requirements.txt") else "pyproject.toml"]
    if runner is None and pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            deps = {}
            for k in ("dependencies", "devDependencies"):
                deps.update(data.get(k) or {})
            cfg = glob1("jest.config.*")
            if "vitest" in deps:
                runner, runner_ev = "vitest", ["package.json"]
            elif "@playwright/test" in deps:
                runner, runner_ev = "playwright", ["package.json"]
            elif "jest" in deps or cfg:
                runner, runner_ev = "jest", [x for x in ("package.json", cfg) if x]
        except (json.JSONDecodeError, OSError):
            pass
    if runner is None and has("go.mod"):
        runner, runner_ev = "go test", ["go.mod"]
    if runner is None and has("Cargo.toml"):
        runner, runner_ev = "cargo test", ["Cargo.toml"]

    # ---- typechecker ----
    if has("mypy.ini") or "[tool.mypy]" in pydeps:
        checker = "mypy"
        checker_ev = [p for p in ("mypy.ini", "pyproject.toml") if has(p)]
    elif has("pyrightconfig.json"):
        checker, checker_ev = "pyright", ["pyrightconfig.json"]
    elif has("tsconfig.json"):
        checker, checker_ev = "tsc", ["tsconfig.json"]

    # ---- linter ----
    if has("ruff.toml") or "[tool.ruff]" in pydeps:
        linter = "ruff"
        linter_ev = [p for p in ("ruff.toml", "pyproject.toml") if has(p)]
    else:
        es = glob1(".eslintrc*") or glob1("eslint.config.*")
        if es:
            linter, linter_ev = "eslint", [es]
        elif glob1(".golangci*"):
            linter, linter_ev = "golangci-lint", [glob1(".golangci*")]
        elif has(".flake8") or has(".pylintrc"):
            linter = "flake8/pylint"
            linter_ev = [p for p in (".flake8", ".pylintrc") if has(p)]

    config_paths = sorted(set(fw_ev + runner_ev + checker_ev + linter_ev))
    return {
        "framework": fw, "test_runner": runner, "typechecker": checker,
        "linter": linter, "config_paths": config_paths,
    }


TEST_PATTERNS = (
    re.compile(r"(^|/)test_[^/]*\.py$"),
    re.compile(r"(^|/)[^/]*_test\.py$"),
    re.compile(r"(^|/)[^/]*\.test\.[^/]+$"),
    re.compile(r"(^|/)[^/]*\.spec\.[^/]+$"),
    re.compile(r"(^|/)[^/]*_test\.go$"),
)


def _test_files(root: Path) -> list[str]:
    out = []
    for rel in _walk_files(root):
        if any(p.search(rel) for p in TEST_PATTERNS) or "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            if Path(rel).suffix.lower() in TEXT_EXTS or "." not in Path(rel).name:
                out.append(rel)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# import graph
# ---------------------------------------------------------------------------

_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([.\w]+)\s+import\s+([\w\s,.*]+)|import\s+([.\w]+))",
    re.MULTILINE)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"]*?\s+from\s+)?|require\(\s*)['"]([^'"]+)['"]""")

_SRC_ROOTS = ("", "src", "lib", "app")


def _resolve_py(root: Path, importer: Path, mod: str, is_from: bool) -> str | None:
    dots = len(mod) - len(mod.lstrip("."))
    mod = mod.lstrip(".")
    parts = mod.split(".") if mod else []
    if dots:  # relative import: anchor at importer's dir, walk up dots-1
        base = importer.parent
        for _ in range(dots - 1):
            base = base.parent
        candidates = [base]
    else:
        candidates = [root / s for s in _SRC_ROOTS]
    for base in candidates:
        target = base.joinpath(*parts) if parts else base
        for cand in (target.with_suffix(".py"), target / "__init__.py"):
            try:
                if cand.is_file():
                    return cand.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
    return None


_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_js(root: Path, importer: Path, spec: str) -> str | None:
    if not spec.startswith("."):
        return None  # package import, not a repo file
    target = (importer.parent / spec)
    cands = [target.with_suffix(e) for e in _JS_EXTS]
    cands += [target / f"index{e}" for e in _JS_EXTS]
    if target.suffix.lower() in _JS_EXTS:
        cands.insert(0, target)
    for cand in cands:
        try:
            if cand.is_file():
                return cand.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _import_edges(root: Path) -> list[list[str]]:
    edges: set[tuple[str, str]] = set()
    for rel in _walk_files(root):
        ext = Path(rel).suffix.lower()
        if ext == ".py":
            text = _read_text(root / rel, cap=1024 * 1024)
            if text is None:
                continue
            for m in _PY_IMPORT_RE.finditer(text):
                if m.group(3):  # plain `import x.y`
                    mods = [m.group(3)]
                else:  # `from x import a, b` -> try x.a / x.b submodules, else x
                    base, names = m.group(1) or "", m.group(2) or ""
                    if not base:
                        continue
                    mods = []
                    for nm in re.split(r"[\s,]+", names):
                        if nm and nm != "*" and re.match(r"^\w+$", nm):
                            mods.append(f"{base}.{nm}")
                    mods.append(base)
                for mod in mods:
                    hit = _resolve_py(root, root / rel, mod, bool(m.group(1)))
                    if hit and hit != rel:
                        edges.add((rel, hit))
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            text = _read_text(root / rel, cap=1024 * 1024)
            if text is None:
                continue
            for m in _JS_IMPORT_RE.finditer(text):
                hit = _resolve_js(root, root / rel, m.group(1))
                if hit and hit != rel:
                    edges.add((rel, hit))
    return [list(e) for e in sorted(edges)]


# ---------------------------------------------------------------------------
# keyword hit map
# ---------------------------------------------------------------------------

def _keywords(request: str, cap: int = 12) -> list[str]:
    seen, out = set(), []
    for tok in re.split(r"[^A-Za-z0-9]+", request.lower()):
        if len(tok) < 4 or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= cap:
            break
    return out


def _keyword_hits(root: Path, keywords: list[str]) -> dict[str, dict[str, int]]:
    hits: dict[str, dict[str, int]] = {kw: {} for kw in keywords}
    if not keywords:
        return hits
    pats = {kw: re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords}
    for rel in _walk_files(root):
        text = _read_text(root / rel, cap=1024 * 1024)
        if text is None:
            continue
        top = rel.split("/", 1)[0] if "/" in rel else "."
        for kw, pat in pats.items():
            n = len(pat.findall(text))
            if n:
                hits[kw][top] = hits[kw].get(top, 0) + n
    return hits


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def generate_probe(repo_path: Path, request: str, out_md: Path, out_json: Path) -> dict:
    """Scan repo_path and write probe.md (<= ~8KB) and probe.json. Returns the data dict."""
    root = Path(repo_path).resolve()
    all_files = list(_walk_files(root))

    tree = _tree_lines(root)
    loc = _loc_by_lang(root)
    manifests = _manifest_section(root)
    det = _detect(root)
    gitlog = _git(root, "log", "--oneline", "-30").strip() or "(no git log available)"
    tests = _test_files(root)
    edges = _import_edges(root)
    kws = _keywords(request)
    hits = _keyword_hits(root, kws)

    loc_lines = [f"{lang}: {n}" for lang, n in loc.items()] or ["(none)"]
    det_lines = [
        f"framework: {det['framework'] or '(none detected)'}",
        f"test_runner: {det['test_runner'] or '(none detected)'}",
        f"typechecker: {det['typechecker'] or '(none detected)'}",
        f"linter: {det['linter'] or '(none detected)'}",
        "config_paths: " + (", ".join(det["config_paths"]) or "(none)"),
    ]
    edge_lines_all = [f"{a} -> {b}" for a, b in edges]
    edge_lines = edge_lines_all[:200]
    if len(edge_lines_all) > 200:
        edge_lines.append(f"... ({len(edge_lines_all) - 200} more edges in probe.json)")
    kw_lines = []
    for kw in kws:
        per = hits.get(kw) or {}
        if per:
            inner = ", ".join(f"{d}: {c}" for d, c in
                              sorted(per.items(), key=lambda kv: -kv[1]))
            kw_lines.append(f"{kw}: {inner} (total {sum(per.values())})")
        else:
            kw_lines.append(f"{kw}: (no hits)")
    if not kw_lines:
        kw_lines = ["(no salient keywords in request)"]

    def assemble(tree_l, loc_l):
        sections = [
            "## Tree\n" + "\n".join(tree_l),
            "## LOC by language\n" + "\n".join(loc_l),
            "## Manifests\n" + manifests,
            "## Detections\n" + "\n".join(det_lines),
            "## Git log\n" + (gitlog or "(none)"),
            "## Test files\n" + ("\n".join(tests) if tests else "(none found)"),
            "## Import graph\n" + ("\n".join(edge_lines) if edge_lines else "(no intra-repo import edges)"),
            "## Keyword hit map\n" + "\n".join(kw_lines),
        ]
        return "\n\n".join(sections) + "\n"

    md = assemble(tree, loc_lines)
    if len(md.encode("utf-8", errors="replace")) > MD_BUDGET:
        # truncate Tree first, then LOC, never manifests/keywords
        t = tree
        while len(t) > 3 and len(md.encode("utf-8", errors="replace")) > MD_BUDGET:
            t = t[: max(3, int(len(t) * 0.7))]
            md = assemble(t + ["... (truncated)"], loc_lines)
        l = loc_lines
        while len(l) > 3 and len(md.encode("utf-8", errors="replace")) > MD_BUDGET:
            l = l[: max(3, int(len(l) * 0.7))]
            md = assemble(t + ["... (truncated)"], l + ["... (truncated)"])

    data = {
        "detections": det,
        "import_edges": edges,
        "test_files": tests,
        "keyword_hits": hits,
        "loc": loc,
        "file_count": len(all_files),
    }

    out_md = Path(out_md)
    out_json = Path(out_json)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    out_json.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    import sys
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    req = sys.argv[2] if len(sys.argv) > 2 else ""
    outdir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.cwd()
    generate_probe(repo, req, outdir / "probe.md", outdir / "probe.json")
    print(f"wrote {outdir / 'probe.md'} and {outdir / 'probe.json'}")
