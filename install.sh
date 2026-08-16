#!/usr/bin/env bash
# GIGGA v2 one-liner install:
#   curl -fsSL https://raw.githubusercontent.com/conan-8/giggaV2/main/install.sh | bash
#
# Picks the best available installer (uv > pipx > pip --user) and installs the
# gigga2 CLI straight from GitHub. Requires Python 3.9+ and, at run time, the
# opencode CLI (https://opencode.ai) with a configured provider.

set -euo pipefail

REPO="https://github.com/conan-8/giggaV2.git"
PKG="git+${REPO}"

echo "==> Installing gigga2 from ${REPO}"

if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$PKG"
elif command -v pipx >/dev/null 2>&1; then
    pipx install --force "$PKG"
elif command -v python3 >/dev/null 2>&1; then
    python3 -m pip install --user --upgrade "$PKG"
elif command -v python >/dev/null 2>&1; then
    python -m pip install --user --upgrade "$PKG"
else
    echo "error: need uv, pipx, or python with pip to install gigga2" >&2
    exit 1
fi

if ! command -v gigga2 >/dev/null 2>&1; then
    echo "warning: 'gigga2' is not on PATH yet — check your tool bin dir" >&2
fi

if ! command -v opencode >/dev/null 2>&1; then
    cat >&2 <<'EOF'
warning: the opencode CLI was not found.
gigga2 dispatches its agents through opencode — install it from
https://opencode.ai and configure a provider before running a pipeline.
EOF
fi

echo "==> Done. Try: gigga2 --help"
