"""Prompt Generation for Task-Specific Ralph Instances

Generates focused, graded prompts following Ralph Loop principles:
- Specific files and line numbers
- Clear acceptance criteria
- Test requirements with examples
- Edge cases identified
- Implementation patterns
"""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_task_prompt(
    task_id: str,
    task_description: str,
    context: dict[str, Any] | None = None
) -> str:
    """Generate focused, task-specific prompt for Ralph.

    Args:
        task_id: Task identifier (e.g., "T1.2")
        task_description: Original task description from @fix_plan.md
        context: Optional context dict with:
            - repo_path: Path to repository
            - project_name: Name of project
            - related_files: List of related file paths
            - patterns: Dict of code patterns to follow

    Returns:
        Markdown-formatted prompt ready for Ralph to execute

    Example output:
        # Task T1.2: Preserve Slack Metadata

        ## Goal
        Store Slack channel_id, thread_ts, user_id in capture.source_metadata JSON field.

        ## Files
        - `scheduler.py` line 257 (poll_slack_for_tasks function)
        - `database.py` line 45 (Capture model)

        ## Implementation Pattern
        Follow existing pattern in `scheduler.py` line 180 for Jira metadata:
        ```python
        metadata = {
            "channel_id": message.get("channel"),
            "thread_ts": message.get("thread_ts"),
            "user_id": message.get("user")
        }
        ```

        ## Acceptance Criteria
        - [ ] Slack captures include all 3 metadata fields
        - [ ] Fields persist to DB (verified in test)
        - [ ] Translation layer can access metadata
        - [ ] Null handling: if field missing, store null

        ## Edge Cases
        - Thread messages vs. direct messages (thread_ts may be null)
        - Bot messages (user_id may be bot ID)

        ## Tests Required
        ```python
        def test_slack_metadata_persisted():
            # Create capture with metadata
            # Verify all fields in DB
            # Verify translation can read them
        ```

        ## Success
        All checkboxes checked, tests pass, no errors in last 2 loops.
    """
    context = context or {}

    # Extract title from description (usually first line)
    lines = task_description.strip().split('\n')
    title = lines[0].strip('- [ ] ').strip('- [x] ')

    # Build prompt sections
    prompt = f"# Task {task_id}: {title}\n\n"

    # Goal section - use enriched description if available
    prompt += "## Goal\n"
    if context.get('description'):
        prompt += f"{context['description']}\n\n"
    else:
        prompt += f"{task_description}\n\n"

    # Files section (prefer extracted file_paths, fall back to related_files)
    file_paths = context.get('file_paths') or context.get('related_files')
    if file_paths:
        prompt += "## Files\n"
        for file_path in file_paths:
            prompt += f"- `{file_path}`\n"
        prompt += "\n"

    # Implementation Pattern section (if context provides patterns)
    if context.get('patterns'):
        prompt += "## Implementation Pattern\n"
        for pattern_name, pattern_code in context['patterns'].items():
            prompt += f"{pattern_name}:\n```python\n{pattern_code}\n```\n\n"

    # Code examples from fix plan (if available)
    if context.get('code_blocks'):
        prompt += "## Code Examples\n"
        prompt += "The following code blocks from the spec provide implementation guidance:\n\n"
        for block in context['code_blocks']:
            prompt += f"{block}\n\n"

    # Dependencies section (if available)
    if context.get('depends_on'):
        prompt += "## Dependencies\n"
        prompt += "This task depends on the completion of:\n"
        for dep in context['depends_on']:
            prompt += f"- {dep}\n"
        prompt += "\n"

    # Acceptance Criteria section
    prompt += "## Acceptance Criteria\n"
    # Use enriched description for criteria extraction if available
    criteria_source = context.get('description', task_description)
    criteria = _extract_acceptance_criteria(criteria_source)
    if criteria:
        for criterion in criteria:
            prompt += f"- [ ] {criterion}\n"
    else:
        # Generate basic criteria from description
        prompt += "- [ ] Implementation complete\n"
        prompt += "- [ ] Tests pass\n"
        prompt += "- [ ] No errors in last 2 loops\n"
    prompt += "\n"

    # Edge Cases section
    prompt += "## Edge Cases\n"
    edge_cases = _infer_edge_cases(task_description)
    if edge_cases:
        for case in edge_cases:
            prompt += f"- {case}\n"
    else:
        prompt += "- Consider null/undefined values\n"
        prompt += "- Consider empty collections\n"
        prompt += "- Consider error conditions\n"
    prompt += "\n"

    # Tests Required section
    prompt += "## Tests Required\n"
    test_name = _generate_test_name(title)
    prompt += "```python\n"
    prompt += f"def {test_name}():\n"
    prompt += "    # TODO: Implement test\n"
    prompt += "    pass\n"
    prompt += "```\n\n"

    # Success criteria
    prompt += "## Success\n"
    prompt += "All checkboxes checked, tests pass, no errors in last 2 loops.\n"

    return prompt


def _extract_acceptance_criteria(description: str) -> list[str]:
    """Extract acceptance criteria from task description.

    Looks for patterns like:
    - Must do X
    - Should handle Y
    - Ensure Z
    """
    criteria = []

    # Look for bullet points that indicate requirements
    lines = description.split('\n')
    for line in lines:
        line = line.strip()

        # Skip empty lines and main task line
        if not line or line.startswith('- [ ]') or line.startswith('- [x]'):
            continue

        # Look for requirement indicators
        if any(word in line.lower() for word in ['must', 'should', 'ensure', 'verify', 'check']):
            criteria.append(line.strip('- '))

    return criteria


def _infer_edge_cases(description: str) -> list[str]:
    """Infer potential edge cases from task description.

    Returns:
        List of edge case descriptions
    """
    edge_cases = []
    desc_lower = description.lower()

    # Common edge case patterns
    if 'null' in desc_lower or 'optional' in desc_lower:
        edge_cases.append("Null/undefined values")

    if 'empty' in desc_lower or 'missing' in desc_lower:
        edge_cases.append("Empty collections or missing data")

    if 'api' in desc_lower or 'request' in desc_lower:
        edge_cases.append("Network errors or timeouts")

    if 'database' in desc_lower or 'db' in desc_lower:
        edge_cases.append("Database connection failures")

    if 'concurrent' in desc_lower or 'parallel' in desc_lower:
        edge_cases.append("Race conditions")

    if 'file' in desc_lower or 'path' in desc_lower:
        edge_cases.append("File not found or permission denied")

    return edge_cases


def _generate_test_name(title: str) -> str:
    """Generate a test function name from task title.

    Args:
        title: Task title

    Returns:
        Snake_case test function name

    Example:
        "Preserve Slack Metadata" -> "test_preserve_slack_metadata"
    """
    # Remove special characters and convert to snake_case
    name = re.sub(r'[^\w\s]', '', title)
    name = re.sub(r'\s+', '_', name.strip())
    name = name.lower()

    return f"test_{name}"


def expand_task_with_codebase_context(
    task_id: str,
    task_description: str,
    repo_path: Path
) -> tuple[str, dict[str, Any]]:
    """Expand task description by searching codebase for context.

    This function would ideally use Claude to:
    1. Search for relevant files mentioned in task
    2. Find similar patterns in codebase
    3. Identify related functions/classes
    4. Extract implementation examples

    For now, this is a placeholder that returns basic context.

    Args:
        task_id: Task identifier
        task_description: Original task description
        repo_path: Path to repository root

    Returns:
        Tuple of (expanded_prompt, context_dict)
    """
    context: dict[str, Any] = {
        'repo_path': str(repo_path),
        'project_name': repo_path.name,
        'related_files': [],
        'patterns': {}
    }

    # TODO: Implement codebase search using:
    # - ripgrep for finding mentions of files/functions
    # - tree-sitter for code structure analysis
    # - Claude API for semantic search

    # For now, just generate basic prompt
    prompt = generate_task_prompt(task_id, task_description, context)

    return prompt, context
