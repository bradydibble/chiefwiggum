"""Fix plan parser for @fix_plan.md files.

Extracts structured task information from markdown fix plan files,
supporting multiple task format patterns (numbered, ID-based, tier-based, plain).
"""

import logging
import re
from pathlib import Path

from chiefwiggum.models import FixPlanTask, TaskPriority

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:50]


def _generate_task_id(task_number: int, title: str) -> str:
    """Generate task ID from number and title."""
    return f"task-{task_number}-{_slugify(title)}"


def parse_fix_plan(path: str | Path) -> list[FixPlanTask]:
    """Parse @fix_plan.md and return list of tasks.

    Supports multiple formats:

    Format 1 (numbered):
    - Section: `## HIGH PRIORITY - Get Data Flowing`
    - Task: `### 22. File Processing Workflow COMPLETE`
    - Subtask: `- [x] Create file upload endpoint`

    Format 2 (ID-based):
    - Section: `## PRODUCT FEEDBACK (IMMEDIATE PRIORITY)`
    - Task: `#### PF-1: Timezone to Pacific`
    - Subtask: `- [ ] Change timezone setting`

    Args:
        path: Path to the fix_plan.md file

    Returns:
        List of FixPlanTask objects
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"Fix plan not found: {path}")
        return []

    content = path.read_text()
    tasks: list[FixPlanTask] = []

    current_section: str | None = None
    current_priority: TaskPriority | None = None
    current_task: FixPlanTask | None = None
    task_counter = 0  # For ID-based tasks without numbers

    # Priority section patterns - check most specific first
    section_patterns = [
        # Explicit priority keywords
        (r"##\s*HIGH\s*PRIORITY", TaskPriority.HIGH),
        (r"##\s*MEDIUM\s*PRIORITY", TaskPriority.MEDIUM),
        (r"##\s*LOWER\s*PRIORITY", TaskPriority.LOWER),
        (r"##\s*POLISH", TaskPriority.POLISH),
        # Alternative patterns
        (r"##.*IMMEDIATE\s*PRIORITY", TaskPriority.HIGH),
        (r"##.*CRITICAL", TaskPriority.HIGH),
        (r"##.*Tier\s*1", TaskPriority.HIGH),
        (r"##.*Tier\s*2", TaskPriority.MEDIUM),
        (r"##.*Tier\s*3", TaskPriority.LOWER),
        (r"##.*Tier\s*4", TaskPriority.POLISH),
        (r"###\s*Tier\s*1", TaskPriority.HIGH),
        (r"###\s*Tier\s*2", TaskPriority.MEDIUM),
        (r"###\s*Tier\s*3", TaskPriority.LOWER),
        (r"###\s*Tier\s*4", TaskPriority.POLISH),
        # Tier-numbered sections (#### TIER N: ...) as used in @fix_plan.md
        (r"####\s*TIER\s*[01][:\s]", TaskPriority.HIGH),
        (r"####\s*TIER\s*[23][:\s]", TaskPriority.MEDIUM),
        (r"####\s*TIER\s*[45][:\s]", TaskPriority.LOWER),
        (r"####\s*TIER\s*[6-9][:\s]", TaskPriority.POLISH),
    ]

    # Track section/task indices for stable IDs
    section_index = -1
    task_index_in_section = 0
    # Track description accumulation
    description_lines: list[str] = []
    in_code_block = False
    current_code_block_lines: list[str] = []

    for line_num, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()

        # Check for section headers
        section_matched = False
        for pattern, priority in section_patterns:
            if re.match(pattern, stripped, re.IGNORECASE):
                current_priority = priority
                # Extract section name after the priority indicator
                match = re.search(r"[-:]\s*(.+)$", stripped)
                if match:
                    current_section = match.group(1).strip()
                else:
                    # Use the whole line minus ## as section
                    current_section = re.sub(r"^##+\s*", "", stripped).strip()
                section_index += 1
                task_index_in_section = 0
                section_matched = True
                break

        if section_matched:
            continue

        # Check for task headers - multiple formats
        task_match = None
        task_number = None
        title_part = None

        # Format 1: ### N. Title (numbered)
        numbered_match = re.match(r"#{2,4}\s*(\d+)\.\s*(.+)", stripped)
        if numbered_match:
            task_number = int(numbered_match.group(1))
            title_part = numbered_match.group(2)
            task_match = True

        # Format 2: #### ID-N: Title (ID-based like PF-1, BUG-42)
        if not task_match:
            id_match = re.match(r"#{2,4}\s*([A-Z]+-\d+)[:\s]+(.+)", stripped)
            if id_match:
                task_counter += 1
                task_number = task_counter
                title_part = f"{id_match.group(1)}: {id_match.group(2)}"
                task_match = True

        # Format 3: #### Title (no number, just header under a Tier section)
        # Must be a multi-word title to avoid matching section headers, notes, etc.
        _non_task_headers = {
            "note", "notes", "overview", "background", "summary", "important",
            "warning", "example", "examples", "table", "appendix", "reference",
            "references", "detail", "details", "description", "context",
            "tier",
        }
        # Format 4: ##### T0.1: Title  (dot-numbered tier tasks, e.g. @fix_plan.md)
        if not task_match and current_priority:
            tier_task_match = re.match(r"#{2,5}\s*(T\d+\.\d+)[:\s]+(.+)", stripped)
            if tier_task_match:
                task_counter += 1
                task_number = task_counter
                title_part = f"{tier_task_match.group(1)}: {tier_task_match.group(2).strip()}"
                task_match = True

        if not task_match and current_priority:
            plain_match = re.match(r"####\s+([A-Z][^#].{5,})", stripped)  # At least 6 chars, starts with capital
            if plain_match and not plain_match.group(1).startswith("**"):  # Not bold text
                title_candidate = plain_match.group(1).strip()
                first_word = title_candidate.split()[0].lower().rstrip(":")
                # Require multiple words and exclude known non-task header keywords
                if " " in title_candidate and first_word not in _non_task_headers:
                    task_counter += 1
                    task_number = task_counter
                    title_part = title_candidate
                    task_match = True

        if task_match and task_number and title_part and current_priority:
            if current_task:
                # Finalize previous task: flush description and code blocks
                _finalize_task(current_task, description_lines, current_code_block_lines, in_code_block)
                # Before appending, check if task is complete based on subtasks
                # A task is complete if it has subtasks and ALL are checked
                if not current_task.is_complete and current_task.completed_subtasks:
                    if not current_task.subtasks:  # No unchecked subtasks remain
                        current_task.is_complete = True
                tasks.append(current_task)

            # Reset description tracking
            description_lines = []
            in_code_block = False
            current_code_block_lines = []

            # Check if complete: "COMPLETE" in title, or checkmark emoji
            is_complete = (
                "COMPLETE" in title_part.upper() or
                "\u2705" in title_part or
                "\u2713" in title_part
            )

            # Clean title (remove checkmarks and COMPLETE markers)
            title = re.sub(r"\s*[\u2705\u2713]\s*", "", title_part).strip()
            title = re.sub(r"\s*COMPLETE\s*", "", title, flags=re.IGNORECASE).strip()

            # Generate stable ID
            stable_id = f"s{section_index}-t{task_index_in_section}"
            task_index_in_section += 1

            current_task = FixPlanTask(
                task_id=_generate_task_id(task_number, title),
                task_number=task_number,
                title=title,
                priority=current_priority,
                section=current_section,
                is_complete=is_complete,
                subtasks=[],
                completed_subtasks=[],
                stable_id=stable_id,
                source_line=line_num,
            )
            continue

        # Accumulate content under current task
        if current_task:
            # Handle code blocks
            if stripped.startswith("```"):
                if in_code_block:
                    # End of code block
                    current_code_block_lines.append(line)
                    current_task.code_blocks.append("\n".join(current_code_block_lines))
                    current_code_block_lines = []
                    in_code_block = False
                else:
                    # Start of code block
                    in_code_block = True
                    current_code_block_lines = [line]
                continue

            if in_code_block:
                current_code_block_lines.append(line)
                description_lines.append(line)
                continue

            # Check for subtasks (- [ ] or - [x])
            subtask_match = re.match(r"-\s*\[([ x])\]\s*(.+)", stripped)
            if subtask_match:
                is_checked = subtask_match.group(1).lower() == "x"
                subtask_text = subtask_match.group(2).strip()

                if is_checked:
                    current_task.completed_subtasks.append(subtask_text)
                else:
                    current_task.subtasks.append(subtask_text)
                continue

            # Accumulate description lines (non-empty, non-header)
            if stripped and not stripped.startswith("#"):
                description_lines.append(stripped)

    # Don't forget the last task
    if current_task:
        _finalize_task(current_task, description_lines, current_code_block_lines, in_code_block)
        # Check if task is complete based on subtasks
        if not current_task.is_complete and current_task.completed_subtasks:
            if not current_task.subtasks:  # No unchecked subtasks remain
                current_task.is_complete = True
        tasks.append(current_task)

    # Post-processing: extract file paths and dependencies from descriptions
    for task in tasks:
        if task.description:
            task.file_paths = _extract_file_paths(task.description)
            task.depends_on = _extract_dependencies(task.description)

    return tasks


def _finalize_task(
    task: FixPlanTask,
    description_lines: list[str],
    code_block_lines: list[str],
    in_code_block: bool,
) -> None:
    """Finalize a task by flushing accumulated description and code blocks."""
    if description_lines:
        task.description = "\n".join(description_lines).strip()
    # Handle unclosed code block
    if in_code_block and code_block_lines:
        task.code_blocks.append("\n".join(code_block_lines))


# File path extraction patterns
_FILE_PATH_PATTERN = re.compile(
    r'`((?:[a-zA-Z_][\w]*(?:/[\w.\-]+)+|[\w.\-]+\.(?:py|ts|tsx|js|jsx|html|css|scss|sql|yml|yaml|toml|json|md|sh|go|rs|java|kt|swift|rb|php|c|cpp|h|hpp)))`'
)
_FILE_REF_PATTERN = re.compile(
    r'\*\*File:\*\*\s*`?([a-zA-Z_][\w/.\-]+\.\w+)'
)
_FILE_LINE_PATTERN = re.compile(
    r'(?:^|\s)((?:src|lib|app|tests|scripts|migrations|templates|static|frontend|backend|chiefwiggum)/[\w/.\-]+\.\w+)(?::\d+)?'
)


def _extract_file_paths(text: str) -> list[str]:
    """Extract file paths from task description text."""
    paths: set[str] = set()

    for pattern in (_FILE_PATH_PATTERN, _FILE_REF_PATTERN, _FILE_LINE_PATTERN):
        for match in pattern.finditer(text):
            path = match.group(1)
            # Skip version-like strings (e.g., python3.11)
            if re.match(r'^[a-z]+\d+\.\d+$', path):
                continue
            paths.add(path)

    return sorted(paths)


# Dependency extraction patterns
_DEPENDS_PATTERN = re.compile(
    r'(?:depends?\s*on|after|requires|blocked\s*by|prerequisite)[:\s]+'
    r'((?:[A-Z]+-\d+|T\d+\.\d+|task-\d+-[\w-]+|#\d+)(?:\s*,\s*(?:[A-Z]+-\d+|T\d+\.\d+|task-\d+-[\w-]+|#\d+))*)',
    re.IGNORECASE
)
_MUST_COMPLETE_PATTERN = re.compile(
    r'must\s+complete\s+before[:\s]+(.+)',
    re.IGNORECASE
)


def _extract_dependencies(text: str) -> list[str]:
    """Extract dependency references from task description text."""
    deps: set[str] = set()

    for match in _DEPENDS_PATTERN.finditer(text):
        # Split comma-separated references
        refs = match.group(1)
        for ref in re.split(r'\s*,\s*', refs):
            ref = ref.strip()
            if ref:
                deps.add(ref)

    for match in _MUST_COMPLETE_PATTERN.finditer(text):
        # Extract task IDs from "must complete before" text
        refs_text = match.group(1)
        for ref in re.findall(r'[A-Z]+-\d+|T\d+\.\d+|task-\d+-[\w-]+|#\d+', refs_text):
            deps.add(ref)

    return sorted(deps)
