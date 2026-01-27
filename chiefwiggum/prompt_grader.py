"""Prompt Grading using Scout/Quinn/Gafton Perspectives

Grades task prompts to ensure quality before spawning Ralph instances.
Poor prompts waste tokens and lead to cascading errors.

Grading Criteria (0-100):
- files_specific (20pts): Has file:line references
- acceptance_clear (20pts): Has testable checkboxes
- tests_specified (20pts): Test requirements with examples
- edge_cases (15pts): Edge cases identified
- integration_points (10pts): How pieces fit together
- examples_provided (15pts): Code examples or patterns

Personas:
- Scout: Is this understandable to a newcomer?
- Quinn: Can I test this end-to-end?
- Gafton: Will this fail in production?
"""

import logging
import re

logger = logging.getLogger(__name__)


def grade_prompt(prompt: str) -> tuple[int, str]:
    """Grade prompt 0-100 using Scout/Quinn/Gafton perspectives.

    Args:
        prompt: Task-specific prompt to grade

    Returns:
        Tuple of (score, reasoning)

    Example:
        >>> score, reasoning = grade_prompt(task_prompt)
        >>> print(f"Grade: {score}/100")
        Grade: 85/100
        >>> print(reasoning)
        files_specific: 20/20 - Has specific file paths with line numbers
        acceptance_clear: 18/20 - Has checkboxes but some are vague
        tests_specified: 15/20 - Test structure shown but no assertions
        ...
    """
    scores = {}
    reasoning_parts = []

    # 1. Files Specific (20pts) - Scout perspective
    files_score, files_reason = _grade_files_specific(prompt)
    scores['files_specific'] = files_score
    reasoning_parts.append(f"files_specific: {files_score}/20 - {files_reason}")

    # 2. Acceptance Criteria Clear (20pts) - Quinn perspective
    acceptance_score, acceptance_reason = _grade_acceptance_clear(prompt)
    scores['acceptance_clear'] = acceptance_score
    reasoning_parts.append(f"acceptance_clear: {acceptance_score}/20 - {acceptance_reason}")

    # 3. Tests Specified (20pts) - Quinn perspective
    tests_score, tests_reason = _grade_tests_specified(prompt)
    scores['tests_specified'] = tests_score
    reasoning_parts.append(f"tests_specified: {tests_score}/20 - {tests_reason}")

    # 4. Edge Cases (15pts) - Gafton perspective
    edge_score, edge_reason = _grade_edge_cases(prompt)
    scores['edge_cases'] = edge_score
    reasoning_parts.append(f"edge_cases: {edge_score}/15 - {edge_reason}")

    # 5. Integration Points (10pts) - Gafton perspective
    integration_score, integration_reason = _grade_integration_points(prompt)
    scores['integration_points'] = integration_score
    reasoning_parts.append(f"integration_points: {integration_score}/10 - {integration_reason}")

    # 6. Examples Provided (15pts) - Scout perspective
    examples_score, examples_reason = _grade_examples_provided(prompt)
    scores['examples_provided'] = examples_score
    reasoning_parts.append(f"examples_provided: {examples_score}/15 - {examples_reason}")

    total_score = sum(scores.values())
    reasoning = "\n".join(reasoning_parts)

    return total_score, reasoning


def _grade_files_specific(prompt: str) -> tuple[int, str]:
    """Grade: Are specific files with line numbers mentioned?

    20pts: Multiple files with line numbers
    15pts: Multiple files, some with line numbers
    10pts: Files mentioned but no line numbers
    5pts: Vague file references ("the config file")
    0pts: No files mentioned
    """
    # Look for file paths with backticks
    file_patterns = [
        r'`[\w/]+\.[\w]+`',  # `path/to/file.py`
        r'[\w/]+\.[\w]+:\d+',  # file.py:123
        r'line \d+',  # line 123
    ]

    files_found = set()
    line_numbers_found = 0

    for pattern in file_patterns:
        matches = re.findall(pattern, prompt)
        files_found.update(matches)
        if 'line' in pattern or ':' in pattern:
            line_numbers_found += len(matches)

    if len(files_found) >= 2 and line_numbers_found >= 2:
        return 20, "Multiple files with specific line numbers"
    elif len(files_found) >= 2 and line_numbers_found >= 1:
        return 15, "Multiple files, some with line numbers"
    elif len(files_found) >= 1 and line_numbers_found >= 1:
        return 12, "File mentioned with line number"
    elif len(files_found) >= 1:
        return 10, "Files mentioned but no line numbers"
    elif any(word in prompt.lower() for word in ['file', 'module', 'class']):
        return 5, "Vague file references without specific paths"
    else:
        return 0, "No files mentioned"


def _grade_acceptance_clear(prompt: str) -> tuple[int, str]:
    """Grade: Are acceptance criteria clear and testable?

    20pts: Multiple checkboxes with specific, measurable criteria
    15pts: Checkboxes present but some vague
    10pts: Acceptance criteria exist but not in checkbox format
    5pts: Success criteria mentioned but unclear
    0pts: No acceptance criteria
    """
    # Look for checkbox patterns
    checkbox_pattern = r'- \[ \] .+'
    checkboxes = re.findall(checkbox_pattern, prompt)

    if len(checkboxes) >= 4:
        # Check if they're specific (contain verbs like "verify", "ensure", "test")
        specific_count = sum(1 for cb in checkboxes
                           if any(word in cb.lower() for word in
                                  ['verify', 'ensure', 'test', 'check', 'confirm']))
        if specific_count >= 3:
            return 20, f"{len(checkboxes)} specific, measurable checkboxes"
        else:
            return 15, f"{len(checkboxes)} checkboxes but some are vague"
    elif len(checkboxes) >= 2:
        return 12, f"{len(checkboxes)} checkboxes provided"
    elif 'acceptance' in prompt.lower() or 'criteria' in prompt.lower():
        return 10, "Acceptance criteria mentioned but not as checkboxes"
    elif 'success' in prompt.lower():
        return 5, "Success criteria mentioned but unclear"
    else:
        return 0, "No acceptance criteria defined"


def _grade_tests_specified(prompt: str) -> tuple[int, str]:
    """Grade: Are test requirements specified with examples?

    20pts: Test code with structure, assertions, and edge cases
    15pts: Test code structure shown
    10pts: Test names mentioned
    5pts: Testing mentioned generally
    0pts: No test requirements
    """
    # Look for test code blocks
    test_code_pattern = r'```python\s*def test_[\w]+.*?```'
    test_blocks = re.findall(test_code_pattern, prompt, re.DOTALL)

    if test_blocks:
        # Check if tests have assertions
        has_assertions = any(
            any(word in block for word in ['assert', 'assertEqual', 'verify', 'check'])
            for block in test_blocks
        )
        if has_assertions:
            return 20, f"{len(test_blocks)} test(s) with assertions"
        else:
            return 15, f"{len(test_blocks)} test structure(s) shown"
    elif re.search(r'def test_\w+', prompt):
        return 10, "Test function names mentioned"
    elif 'test' in prompt.lower():
        return 5, "Testing mentioned generally"
    else:
        return 0, "No test requirements"


def _grade_edge_cases(prompt: str) -> tuple[int, str]:
    """Grade: Are edge cases identified?

    15pts: Multiple specific edge cases with handling strategy
    10pts: Multiple edge cases listed
    5pts: Edge cases mentioned generally
    0pts: No edge cases considered
    """
    # Look for edge case section
    if '## Edge Cases' in prompt or '## edge cases' in prompt.lower():
        # Count bullet points in edge cases section
        edge_section_match = re.search(
            r'## Edge Cases.*?(?=##|\Z)',
            prompt,
            re.DOTALL | re.IGNORECASE
        )
        if edge_section_match:
            edge_section = edge_section_match.group()
            edge_bullets = re.findall(r'^- .+$', edge_section, re.MULTILINE)

            if len(edge_bullets) >= 3:
                return 15, f"{len(edge_bullets)} specific edge cases identified"
            elif len(edge_bullets) >= 1:
                return 10, f"{len(edge_bullets)} edge case(s) listed"
            else:
                return 5, "Edge cases section exists but empty"

    # Look for edge case keywords
    edge_keywords = ['null', 'empty', 'error', 'timeout', 'race', 'concurrent',
                     'missing', 'invalid', 'boundary', 'overflow']
    edge_count = sum(1 for keyword in edge_keywords if keyword in prompt.lower())

    if edge_count >= 3:
        return 8, f"Edge cases implied ({edge_count} keywords found)"
    elif edge_count >= 1:
        return 5, "Some edge case consideration"
    else:
        return 0, "No edge cases considered"


def _grade_integration_points(prompt: str) -> tuple[int, str]:
    """Grade: Are integration points and system interactions clear?

    10pts: Clear description of how components interact
    7pts: Integration points mentioned
    4pts: Some system context provided
    0pts: Task exists in isolation
    """
    integration_keywords = [
        'integrate', 'connect', 'call', 'api', 'endpoint',
        'database', 'layer', 'system', 'component', 'module',
        'depends on', 'uses', 'interacts with'
    ]

    integration_count = sum(1 for keyword in integration_keywords
                          if keyword in prompt.lower())

    # Look for pattern sections
    has_pattern_section = '## Implementation Pattern' in prompt or '## Pattern' in prompt

    if integration_count >= 5 or has_pattern_section:
        return 10, "Clear integration points and patterns"
    elif integration_count >= 3:
        return 7, "Integration points mentioned"
    elif integration_count >= 1:
        return 4, "Some system context provided"
    else:
        return 0, "Task exists in isolation"


def _grade_examples_provided(prompt: str) -> tuple[int, str]:
    """Grade: Are code examples or patterns provided?

    15pts: Multiple code examples showing patterns to follow
    10pts: One complete code example
    5pts: Code snippets or pseudo-code
    0pts: No examples
    """
    # Look for code blocks
    code_blocks = re.findall(r'```\w+\s*\n.*?```', prompt, re.DOTALL)

    # Filter out test blocks (already counted separately)
    example_blocks = [block for block in code_blocks
                     if 'def test_' not in block]

    if len(example_blocks) >= 2:
        return 15, f"{len(example_blocks)} code examples showing patterns"
    elif len(example_blocks) >= 1:
        return 10, "One code example provided"
    elif '```' in prompt:
        return 5, "Code snippets present"
    else:
        return 0, "No code examples"


def get_grade_color(grade: int | None) -> str:
    """Get ANSI color code for grade display.

    Args:
        grade: Score 0-100

    Returns:
        ANSI color code string
    """
    if grade is None:
        return '\033[90m'  # Gray

    if grade >= 90:
        return '\033[92m'  # Bright green (A)
    elif grade >= 70:
        return '\033[93m'  # Yellow (B)
    elif grade >= 50:
        return '\033[91m'  # Red (C)
    else:
        return '\033[91m\033[1m'  # Bold red (F)


def get_grade_letter(grade: int | None) -> str:
    """Get letter grade from numeric score.

    Args:
        grade: Score 0-100

    Returns:
        Letter grade (A, B, C, F)
    """
    if grade is None:
        return '?'

    if grade >= 90:
        return 'A'
    elif grade >= 70:
        return 'B'
    elif grade >= 50:
        return 'C'
    else:
        return 'F'
