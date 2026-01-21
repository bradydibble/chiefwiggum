#!/usr/bin/env bash
set -euo pipefail

# ChiefWiggum Deployment Script
# Pre-flight checks and package build

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "ChiefWiggum Deployment Pre-flight Checks"
echo "=========================================="
echo ""

# Track failures
FAILURES=0

# 1. Check Python version
echo -n "Checking Python version... "
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -ge 3 ]] && [[ "$PYTHON_MINOR" -ge 11 ]]; then
    echo -e "${GREEN}OK${NC} (Python $PYTHON_VERSION)"
else
    echo -e "${RED}FAIL${NC} (Python $PYTHON_VERSION, need >= 3.11)"
    FAILURES=$((FAILURES + 1))
fi

# 2. Check for uncommitted changes
echo -n "Checking for uncommitted changes... "
cd "$PROJECT_ROOT"
if [[ -z "$(git status --porcelain)" ]]; then
    echo -e "${GREEN}OK${NC} (clean working directory)"
else
    echo -e "${YELLOW}WARNING${NC} (uncommitted changes detected)"
    git status --short
fi

# 3. Check version sync
echo -n "Checking version sync... "
VERSION_FILE_VERSION=$(grep -oP '__version__ = "\K[^"]+' chiefwiggum/_version.py 2>/dev/null || echo "")
if [[ -n "$VERSION_FILE_VERSION" ]]; then
    echo -e "${GREEN}OK${NC} (version $VERSION_FILE_VERSION)"
else
    echo -e "${RED}FAIL${NC} (could not read version from _version.py)"
    FAILURES=$((FAILURES + 1))
fi

# 4. Run tests
echo ""
echo "Running tests..."
echo "----------------"
if python3 -m pytest tests/ -v --tb=short 2>&1; then
    echo -e "${GREEN}Tests passed${NC}"
else
    echo -e "${RED}Tests failed${NC}"
    FAILURES=$((FAILURES + 1))
fi

# 5. Run type checking (if mypy installed)
echo ""
echo -n "Running type check... "
if command -v mypy &> /dev/null; then
    if mypy chiefwiggum/ --ignore-missing-imports --no-error-summary 2>&1 | grep -q "error:"; then
        echo -e "${RED}FAIL${NC} (type errors found)"
        mypy chiefwiggum/ --ignore-missing-imports 2>&1 | head -20
        FAILURES=$((FAILURES + 1))
    else
        echo -e "${GREEN}OK${NC}"
    fi
else
    echo -e "${YELLOW}SKIPPED${NC} (mypy not installed)"
fi

# 6. Run linting (if ruff installed)
echo -n "Running linter... "
if command -v ruff &> /dev/null; then
    if ruff check chiefwiggum/ tests/ --quiet 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC} (lint errors found)"
        ruff check chiefwiggum/ tests/ 2>&1 | head -20
        FAILURES=$((FAILURES + 1))
    fi
else
    echo -e "${YELLOW}SKIPPED${NC} (ruff not installed)"
fi

# Summary
echo ""
echo "=========================================="
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}All pre-flight checks passed!${NC}"
    echo ""
    echo "Ready to build. Run:"
    echo "  python -m build"
    echo ""
    echo "To publish to PyPI:"
    echo "  twine upload dist/*"
    echo ""
    echo "Or create a git tag to trigger GitHub release:"
    echo "  git tag v$VERSION_FILE_VERSION"
    echo "  git push origin v$VERSION_FILE_VERSION"
else
    echo -e "${RED}$FAILURES pre-flight check(s) failed${NC}"
    echo "Please fix the issues above before deploying."
    exit 1
fi

# Optional: Build package
if [[ "${1:-}" == "--build" ]]; then
    echo ""
    echo "Building package..."
    echo "-------------------"
    python3 -m pip install --quiet build
    python3 -m build
    echo ""
    echo -e "${GREEN}Build complete!${NC}"
    echo "Artifacts in dist/"
    ls -la dist/
fi
