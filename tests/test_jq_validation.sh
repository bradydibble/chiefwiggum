#!/bin/bash
# Test suite for jq variable validation in response_analyzer.sh
# Tests that variables are properly validated before jq calls to prevent crashes

set -euo pipefail

# Source the response analyzer to get the validation function
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../chiefwiggum/scripts/lib" && pwd)"
source "$SCRIPT_DIR/response_analyzer.sh"

# Colors for test output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Test helper function
run_test() {
    local test_name=$1
    local expected=$2
    local actual=$3

    TESTS_RUN=$((TESTS_RUN + 1))

    if [[ "$actual" == "$expected" ]]; then
        echo -e "${GREEN}✓${NC} $test_name"
        TESTS_PASSED=$((TESTS_PASSED + 1))
        return 0
    else
        echo -e "${RED}✗${NC} $test_name"
        echo -e "  Expected: $expected"
        echo -e "  Actual:   $actual"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        return 1
    fi
}

echo "Testing jq variable validation..."
echo ""

# =============================================================================
# NUMBER VALIDATION TESTS
# =============================================================================

echo "Number Validation Tests:"

# Test valid numbers
validated=$(validate_for_argjson "test" "0" "number")
run_test "Valid number: 0" "0" "$validated"

validated=$(validate_for_argjson "test" "123" "number")
run_test "Valid number: 123" "123" "$validated"

validated=$(validate_for_argjson "test" "999999" "number")
run_test "Valid number: 999999" "999999" "$validated"

# Test empty string
validated=$(validate_for_argjson "test" "" "number")
run_test "Empty string defaults to 0" "0" "$validated"

# Test non-numeric strings
validated=$(validate_for_argjson "test" "abc" "number")
run_test "Non-numeric string defaults to 0" "0" "$validated"

validated=$(validate_for_argjson "test" "12.5" "number")
run_test "Decimal number defaults to 0" "0" "$validated"

validated=$(validate_for_argjson "test" "-5" "number")
run_test "Negative number defaults to 0" "0" "$validated"

validated=$(validate_for_argjson "test" "1a2b3" "number")
run_test "Mixed alphanumeric defaults to 0" "0" "$validated"

# Test whitespace
validated=$(validate_for_argjson "test" "  " "number")
run_test "Whitespace defaults to 0" "0" "$validated"

# =============================================================================
# BOOLEAN VALIDATION TESTS
# =============================================================================

echo ""
echo "Boolean Validation Tests:"

# Test valid booleans
validated=$(validate_for_argjson "test" "true" "boolean")
run_test "Valid boolean: true" "true" "$validated"

validated=$(validate_for_argjson "test" "false" "boolean")
run_test "Valid boolean: false" "false" "$validated"

# Test invalid booleans (should default to false)
validated=$(validate_for_argjson "test" "" "boolean")
run_test "Empty string defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "1" "boolean")
run_test "Number 1 defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "0" "boolean")
run_test "Number 0 defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "yes" "boolean")
run_test "String 'yes' defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "no" "boolean")
run_test "String 'no' defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "TRUE" "boolean")
run_test "Uppercase 'TRUE' defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "FALSE" "boolean")
run_test "Uppercase 'FALSE' defaults to false" "false" "$validated"

validated=$(validate_for_argjson "test" "maybe" "boolean")
run_test "String 'maybe' defaults to false" "false" "$validated"

# =============================================================================
# INTEGRATION TEST: JQ CALLS WITH VALIDATED VARIABLES
# =============================================================================

echo ""
echo "Integration Tests:"

# Test that validated variables work with jq --argjson
loop_number=""
loop_number=$(validate_for_argjson "loop_number" "$loop_number" "number")
if result=$(jq -n --argjson loop "$loop_number" '{loop: $loop}' 2>/dev/null); then
    run_test "Empty loop_number validated and used in jq" "true" "true"
else
    run_test "Empty loop_number validated and used in jq" "true" "false"
fi

# Test with invalid boolean
exit_signal="maybe"
exit_signal=$(validate_for_argjson "exit_signal" "$exit_signal" "boolean")
if result=$(jq -n --argjson exit "$exit_signal" '{exit: $exit}' 2>/dev/null); then
    run_test "Invalid boolean validated and used in jq" "true" "true"
else
    run_test "Invalid boolean validated and used in jq" "true" "false"
fi

# Test with non-numeric string
files_modified="abc123"
files_modified=$(validate_for_argjson "files_modified" "$files_modified" "number")
if result=$(jq -n --argjson files "$files_modified" '{files: $files}' 2>/dev/null); then
    run_test "Non-numeric string validated and used in jq" "true" "true"
else
    run_test "Non-numeric string validated and used in jq" "true" "false"
fi

# Test complex jq call with all validated variables (simulates actual usage)
loop_number=""
files_modified="not-a-number"
confidence_score="abc"
exit_signal="yes"
has_completion_signal=""

loop_number=$(validate_for_argjson "loop_number" "$loop_number" "number")
files_modified=$(validate_for_argjson "files_modified" "$files_modified" "number")
confidence_score=$(validate_for_argjson "confidence_score" "$confidence_score" "number")
exit_signal=$(validate_for_argjson "exit_signal" "$exit_signal" "boolean")
has_completion_signal=$(validate_for_argjson "has_completion_signal" "$has_completion_signal" "boolean")

if result=$(jq -n \
    --argjson loop_number "$loop_number" \
    --argjson files_modified "$files_modified" \
    --argjson confidence_score "$confidence_score" \
    --argjson exit_signal "$exit_signal" \
    --argjson has_completion_signal "$has_completion_signal" \
    '{
        loop_number: $loop_number,
        files_modified: $files_modified,
        confidence_score: $confidence_score,
        exit_signal: $exit_signal,
        has_completion_signal: $has_completion_signal
    }' 2>/dev/null); then
    run_test "Complex jq call with all invalid inputs validated" "true" "true"

    # Verify the values are correct
    loop_val=$(echo "$result" | jq -r '.loop_number')
    files_val=$(echo "$result" | jq -r '.files_modified')
    conf_val=$(echo "$result" | jq -r '.confidence_score')
    exit_val=$(echo "$result" | jq -r '.exit_signal')
    comp_val=$(echo "$result" | jq -r '.has_completion_signal')

    run_test "Complex jq: loop_number=0" "0" "$loop_val"
    run_test "Complex jq: files_modified=0" "0" "$files_val"
    run_test "Complex jq: confidence_score=0" "0" "$conf_val"
    run_test "Complex jq: exit_signal=false" "false" "$exit_val"
    run_test "Complex jq: has_completion_signal=false" "false" "$comp_val"
else
    run_test "Complex jq call with all invalid inputs validated" "true" "false"
fi

# =============================================================================
# SUMMARY
# =============================================================================

echo ""
echo "================================"
echo "Test Summary:"
echo "  Total:  $TESTS_RUN"
echo -e "  ${GREEN}Passed: $TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "  ${RED}Failed: $TESTS_FAILED${NC}"
else
    echo -e "  ${GREEN}Failed: $TESTS_FAILED${NC}"
fi
echo "================================"

if [[ $TESTS_FAILED -eq 0 ]]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed${NC}"
    exit 1
fi
