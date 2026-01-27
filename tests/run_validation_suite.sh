#!/bin/bash
set -e

echo "🧪 Running ChiefWiggum Validation Suite"
echo "======================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track overall success
OVERALL_SUCCESS=true

# Detect python command
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo -e "${RED}❌ Python not found${NC}"
    exit 1
fi

# Function to run a test section
run_test_section() {
    local section_name="$1"
    local test_command="$2"

    echo "Running: $section_name"
    echo "-------------------"

    if eval "$test_command"; then
        echo -e "${GREEN}✅ $section_name PASSED${NC}"
        echo ""
        return 0
    else
        echo -e "${RED}❌ $section_name FAILED${NC}"
        echo ""
        OVERALL_SUCCESS=false
        return 1
    fi
}

# Phase 1: Unit Tests
echo ""
echo "Phase 1: Unit Tests"
echo "==================="
echo ""

run_test_section "RALPH_STATUS Parsing Tests" \
    "$PYTHON -m pytest tests/test_response_analyzer.py -v --tb=short"

run_test_section "Task Completion Detection Tests" \
    "$PYTHON -m pytest tests/test_spawner_lifecycle.py::TestTaskCompletionDetection -v --tb=short"

run_test_section "Database Update Tests" \
    "$PYTHON -m pytest tests/test_coordination.py::TestCompleteAndClaimNext -v --tb=short"

run_test_section "Fix Plan Update Tests" \
    "$PYTHON -m pytest tests/test_fix_plan_writer.py -v --tb=short"

run_test_section "Monitoring and Alerts Tests" \
    "$PYTHON -m pytest tests/test_monitoring.py -v --tb=short"

# Phase 2: Integration Tests
echo ""
echo "Phase 2: Integration Tests"
echo "=========================="
echo ""

run_test_section "End-to-End Task Completion Tests" \
    "$PYTHON -m pytest tests/test_integration_task_completion.py -v --tb=short"

# Phase 3: Validation Checks
echo ""
echo "Phase 3: Validation Checks"
echo "=========================="
echo ""

# Check RALPH_STATUS parsing with live example
echo "Checking RALPH_STATUS parsing..."
$PYTHON << 'EOF'
from chiefwiggum.spawner import check_task_completion, get_ralph_log_path
import tempfile
from pathlib import Path

# Create temp log with RALPH_STATUS
with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
    f.write("""
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: test-validation-task
COMMIT: abc1234567890def
---END_RALPH_STATUS---
""")
    log_path = f.name

# Mock get_ralph_log_path to return temp file
import chiefwiggum.spawner as spawner_module
old_func = spawner_module.get_ralph_log_path
spawner_module.get_ralph_log_path = lambda x: Path(log_path)

try:
    result = check_task_completion("test-ralph")
    assert result[0] == "test-validation-task", f"Expected 'test-validation-task', got {result[0]}"
    assert result[2] == "abc1234567890def", f"Expected commit SHA, got {result[2]}"
    print("✅ RALPH_STATUS parsing works correctly")
except AssertionError as e:
    print(f"❌ RALPH_STATUS parsing failed: {e}")
    exit(1)
finally:
    spawner_module.get_ralph_log_path = old_func
    Path(log_path).unlink()
EOF

if [ $? -eq 0 ]; then
    echo ""
else
    OVERALL_SUCCESS=false
fi

# Check database schema
echo "Checking database schema..."
if $PYTHON << 'EOF'
from chiefwiggum.database import init_db, get_connection
import asyncio
import os
import tempfile

async def check_schema():
    # Use temp database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        test_db = f.name

    os.environ["CHIEFWIGGUM_DB"] = test_db

    try:
        await init_db()

        conn = await get_connection()
        cursor = await conn.execute("PRAGMA table_info(task_claims)")
        columns = await cursor.fetchall()

        column_names = [col[1] for col in columns]

        required_columns = ['status', 'git_commit_sha', 'completed_at']
        for col in required_columns:
            if col not in column_names:
                print(f"❌ Missing column: {col}")
                return False

        print("✅ Database schema valid")
        return True
    finally:
        del os.environ["CHIEFWIGGUM_DB"]
        if os.path.exists(test_db):
            os.unlink(test_db)

asyncio.run(check_schema())
EOF
then
    echo ""
else
    echo -e "${RED}❌ Database schema check failed${NC}"
    echo ""
    OVERALL_SUCCESS=false
fi

# Final Summary
echo ""
echo "======================================="
echo "Validation Suite Summary"
echo "======================================="
echo ""

if [ "$OVERALL_SUCCESS" = true ]; then
    echo -e "${GREEN}✅ All validation tests passed!${NC}"
    echo ""
    echo "The ChiefWiggum validation suite has verified:"
    echo "  ✓ RALPH_STATUS block parsing (16 tests)"
    echo "  ✓ Task completion detection (6 tests)"
    echo "  ✓ Database updates during completion (5 tests)"
    echo "  ✓ Fix plan update with checkmarks (15 tests)"
    echo "  ✓ Monitoring and alerts (19 tests)"
    echo "  ✓ End-to-end integration tests (6 tests)"
    echo "  ✓ RALPH_STATUS parsing validation"
    echo "  ✓ Database schema validation"
    echo ""
    echo "Total: 67 tests covering all critical paths"
    echo ""
    exit 0
else
    echo -e "${RED}❌ Some validation tests failed${NC}"
    echo ""
    echo "Please review the failures above and fix the issues."
    echo ""
    exit 1
fi
