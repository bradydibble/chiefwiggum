# Ralph-Chiefwiggum Task Integration Spec

## Problem Statement

**Chiefwiggum's core purpose is broken.** The TUI shows tasks and Ralphs, but they're completely disconnected:

1. Ralph (ralph_loop.sh) runs Claude with @fix_plan.md
2. Chiefwiggum parses @fix_plan.md into a task database
3. **These two systems NEVER talk to each other**
4. Tasks stay "pending" forever in the TUI even while Ralph is actively working on them
5. Multiple Ralphs can't coordinate because there's no shared state

## Current Architecture (Broken)

```
┌─────────────────┐     ┌──────────────────┐
│  Chiefwiggum    │     │  ralph_loop.sh   │
│  ┌───────────┐  │     │                  │
│  │ Task DB   │  │     │  Runs Claude     │
│  │ (SQLite)  │  │     │  with fix_plan   │
│  └───────────┘  │     │                  │
│       ↑         │     │       ↓          │
│  sync from      │     │  Edits files     │
│  @fix_plan.md   │     │  directly        │
└─────────────────┘     └──────────────────┘
        ↑                       ↑
        └───── NO CONNECTION ───┘
```

## Required Architecture

```
┌─────────────────────────────────────────────────┐
│                  Chiefwiggum                     │
│  ┌───────────┐      ┌────────────────────────┐  │
│  │ Task DB   │◄────►│ Ralph Orchestrator     │  │
│  │ (SQLite)  │      │ - Assigns tasks        │  │
│  │           │      │ - Tracks progress      │  │
│  │ - tasks   │      │ - Detects completion   │  │
│  │ - claims  │      └────────────────────────┘  │
│  │ - status  │                 │                │
│  └───────────┘                 ▼                │
│                    ┌────────────────────────┐   │
│                    │ Ralph Instance(s)      │   │
│                    │ - Claims ONE task      │   │
│                    │ - Reports progress     │   │
│                    │ - Marks complete/fail  │   │
│                    └────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

## Integration Points

### 1. Task Assignment (Chiefwiggum → Ralph)

When spawning a Ralph:
- Query DB for unclaimed tasks matching targeting (priority, category)
- Claim the task (set `claimed_by_ralph_id`, `claimed_at`)
- Generate a **task-specific prompt** (not the whole fix_plan.md)
- Pass task context to ralph_loop.sh

### 2. Progress Reporting (Ralph → Chiefwiggum)

Ralph needs to report back:
- **Heartbeat**: "I'm still working" (every N seconds)
- **Completion**: "Task done, here's what I did"
- **Failure**: "Task failed, here's the error"
- **Progress**: "Working on step 3 of 5" (optional)

### 3. Task Completion Detection

Options:
a) **Ralph self-reports**: Claude outputs structured status (RALPH_STATUS block)
b) **File watching**: Detect when task's files are modified
c) **Git watching**: Detect commits related to task
d) **Hybrid**: Ralph reports + chiefwiggum verifies

### 4. Multi-Ralph Coordination

Multiple Ralphs on same project:
- Each claims DIFFERENT tasks (DB enforces uniqueness)
- File-level locking to prevent conflicts
- Stale claim expiration (if Ralph dies, release claim after N minutes)

---

## Implementation Plan

### Phase 1: Task-Specific Prompts (Critical)

**Current**: Ralph gets entire @fix_plan.md
**New**: Ralph gets ONE task extracted from fix_plan

```python
# spawner.py changes
def spawn_ralph_for_task(task_id: str, ralph_id: str):
    task = get_task(task_id)
    claim_task(ralph_id, task_id)

    # Generate task-specific prompt
    prompt = f"""
    # Task: {task.title}

    {task.description}

    ## Acceptance Criteria
    {task.criteria}

    ## Files to modify
    {task.files}

    When complete, output:
    TASK_COMPLETE: {task_id}
    """

    # Write to temp file, pass to ralph_loop.sh
    spawn_ralph_daemon(..., prompt_file=prompt_path)
```

### Phase 2: Completion Detection

ralph_loop.sh already parses Claude output. Add:

```bash
# In response_analyzer.sh
if grep -q "TASK_COMPLETE:" "$output_file"; then
    task_id=$(grep "TASK_COMPLETE:" "$output_file" | cut -d: -f2)
    # Signal chiefwiggum via file/socket/API
    echo "$task_id" >> ~/.chiefwiggum/completed_tasks
fi
```

Chiefwiggum watches for completions:
```python
async def check_completed_tasks():
    completed_file = Path.home() / ".chiefwiggum" / "completed_tasks"
    if completed_file.exists():
        for task_id in completed_file.read_text().splitlines():
            await mark_task_completed(task_id)
        completed_file.unlink()
```

### Phase 3: Heartbeat & Progress

Ralph writes status file:
```bash
# ralph_loop.sh writes every loop iteration
echo '{"ralph_id": "...", "task_id": "...", "loop": 5, "status": "working"}' \
    > ~/.chiefwiggum/ralphs/{ralph_id}.status
```

Chiefwiggum reads status files in TUI refresh loop.

### Phase 4: Multi-Ralph Support

- Remove "already running on project" check (current blocker)
- Add "already claimed task" check instead
- Each Ralph works on ONE task at a time
- Allow N Ralphs on same project, different tasks

---

## Files to Modify

| File | Changes |
|------|---------|
| `spawner.py` | Task-specific prompt generation, remove project-level blocking |
| `coordination.py` | Task claiming logic, completion tracking |
| `tui.py` | Show task<->Ralph mapping, progress |
| `ralph_loop.sh` | Output TASK_COMPLETE markers, write status files |
| `response_analyzer.sh` | Parse completion markers |

---

## Success Criteria

1. ✅ Spawn Ralph on specific task (not whole fix_plan)
2. ✅ TUI shows which Ralph is working on which task
3. ✅ Task status updates when Ralph completes
4. ✅ Multiple Ralphs can work on same project (different tasks)
5. ✅ Stale claims auto-release after timeout

---

## Open Questions

1. Should Ralph work on one task then exit, or pick up next task automatically?
2. How to handle tasks that span multiple files (potential conflicts)?
3. Should we integrate with git commits for completion verification?
