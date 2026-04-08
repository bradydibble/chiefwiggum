"""Tests for ChiefWiggum prompt generator."""

from chiefwiggum.prompt_generator import (
    _extract_acceptance_criteria,
    _generate_test_name,
    _infer_edge_cases,
    generate_task_prompt,
)


def test_generate_prompt_contains_task_id():
    prompt = generate_task_prompt("T1.2", "Implement login feature")
    assert "T1.2" in prompt


def test_generate_prompt_contains_description():
    prompt = generate_task_prompt("T1.1", "Fix the broken authentication flow")
    assert "Fix the broken authentication flow" in prompt


def test_generate_prompt_has_required_sections():
    prompt = generate_task_prompt("T2.0", "Add pagination to list endpoint")
    assert "## Goal" in prompt
    assert "## Acceptance Criteria" in prompt
    assert "## Edge Cases" in prompt
    assert "## Tests Required" in prompt
    assert "## Success" in prompt


def test_extract_acceptance_criteria_finds_must_should():
    description = (
        "Implement feature X.\n"
        "Must do validation.\n"
        "Should verify output.\n"
        "Just a regular line."
    )
    criteria = _extract_acceptance_criteria(description)
    assert any("Must do validation" in c for c in criteria)
    assert any("Should verify output" in c for c in criteria)


def test_extract_acceptance_criteria_finds_ensure_verify():
    description = "Task.\nEnsure all tests pass.\nVerify no regressions.\nOther info."
    criteria = _extract_acceptance_criteria(description)
    assert any("Ensure" in c for c in criteria)
    assert any("Verify" in c for c in criteria)


def test_extract_acceptance_criteria_empty_when_no_indicators():
    description = "Just a simple description with no requirement words."
    criteria = _extract_acceptance_criteria(description)
    assert criteria == []


def test_infer_edge_cases_null_pattern():
    description = "Handle optional field value in the response"
    cases = _infer_edge_cases(description)
    assert any("null" in c.lower() or "undefined" in c.lower() for c in cases)


def test_infer_edge_cases_api_pattern():
    description = "Call external API to fetch user data"
    cases = _infer_edge_cases(description)
    assert any("timeout" in c.lower() or "network" in c.lower() for c in cases)


def test_infer_edge_cases_database_pattern():
    description = "Write results to the database"
    cases = _infer_edge_cases(description)
    assert any("database" in c.lower() or "connection" in c.lower() for c in cases)


def test_infer_edge_cases_empty_collection_pattern():
    description = "Process empty list of items or missing keys"
    cases = _infer_edge_cases(description)
    assert any("empty" in c.lower() or "missing" in c.lower() for c in cases)


def test_generate_test_name_snake_case():
    name = _generate_test_name("Fix Login Bug")
    assert name == "test_fix_login_bug"


def test_generate_test_name_starts_with_test():
    name = _generate_test_name("Preserve Slack Metadata")
    assert name.startswith("test_")


def test_generate_test_name_removes_special_chars():
    name = _generate_test_name("Handle 'edge' cases!")
    assert "'" not in name
    assert "!" not in name
    assert name.startswith("test_")


def test_generate_test_name_no_spaces():
    name = _generate_test_name("Some Task Title")
    assert " " not in name


def test_prompt_includes_related_files_from_context():
    context = {"related_files": ["src/auth.py", "tests/test_auth.py"]}
    prompt = generate_task_prompt("T2.1", "Fix auth bug", context=context)
    assert "src/auth.py" in prompt
    assert "tests/test_auth.py" in prompt


def test_prompt_without_context_still_valid():
    prompt = generate_task_prompt("T3.0", "Refactor scheduler")
    assert "T3.0" in prompt
    assert "## Acceptance Criteria" in prompt


def test_prompt_with_patterns_in_context():
    context = {
        "patterns": {
            "Example pattern": "result = do_thing(x)"
        }
    }
    prompt = generate_task_prompt("T4.0", "Add new handler", context=context)
    assert "do_thing" in prompt
