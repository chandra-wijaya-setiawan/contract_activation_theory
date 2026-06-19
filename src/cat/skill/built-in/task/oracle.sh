#!/usr/bin/env bash
# Default oracle for the 'task' skill.
# Called by CAT after each hypothesis: oracle.sh <workdir>
# Exit 0 = hypothesis passed. Non-zero = failed.
# Stdout/stderr captured as oracle evidence shown to the agent.
#
# Auto-detects the test runner from common project files.
# Override by providing --test / oracle.sh in your project's ./skills/task/.

set -euo pipefail
WORKDIR="${1:-.}"
cd "$WORKDIR"

# pytest — most Python projects
if [ -f "pytest.ini" ] || [ -f "setup.cfg" ] || [ -f "tox.ini" ]; then
    exec python -m pytest --tb=short -q
fi

if [ -f "pyproject.toml" ] && grep -q "\[tool.pytest" pyproject.toml 2>/dev/null; then
    exec python -m pytest --tb=short -q
fi

# npm / yarn
if [ -f "package.json" ] && python3 -c "
import json, sys
pkg = json.load(open('package.json'))
sys.exit(0 if 'test' in pkg.get('scripts', {}) else 1)
" 2>/dev/null; then
    exec npm test
fi

# Makefile test target
if [ -f "Makefile" ] && grep -q "^test:" Makefile 2>/dev/null; then
    exec make test
fi

# tests/ or test/ directory exists — run pytest generically
if [ -d "tests" ] || [ -d "test" ]; then
    exec python -m pytest --tb=short -q
fi

echo "No test runner detected. Add oracle.sh to ./skills/task/ to define your oracle."
exit 1
