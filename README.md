# ChiefWiggum

Multi-Ralph Coordination System for Claude Code instances.

ChiefWiggum orchestrates multiple Ralph (Claude Code) instances working on the same codebase, preventing duplicate work, managing task claiming, and providing visibility into what each Ralph is doing.

## Installation

```bash
pipx install --force /Users/bdibble/claudecode/chiefwiggum
```

For development:
```bash
pip install -e /Users/bdibble/claudecode/chiefwiggum
```

## Quick Start

### 1. Initialize the database

```bash
chiefwiggum init
```

### 2. Sync tasks from a fix plan

```bash
chiefwiggum sync ~/claudecode/tian/@fix_plan.md --project tian
```

### 3. Register a Ralph instance

```bash
chiefwiggum register --name ralph-1 --project tian
```

### 4. Claim a task

```bash
chiefwiggum claim tian --ralph-id arch-ralph-1
```

### 5. View status

```bash
chiefwiggum status
```

### 6. Launch the TUI dashboard

```bash
chiefwiggum tui
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `chiefwiggum status` | Show instances and tasks |
| `chiefwiggum tui` | Launch live TUI dashboard |
| `chiefwiggum register [--name]` | Register this terminal as a Ralph instance |
| `chiefwiggum claim <project>` | Claim next task from project's fix_plan |
| `chiefwiggum complete <task-id>` | Mark task complete |
| `chiefwiggum release <task-id>` | Release claim without completing |
| `chiefwiggum sync <fix_plan>` | Sync tasks from fix_plan.md |
| `chiefwiggum shutdown --ralph-id <id>` | Shutdown a Ralph instance |
| `chiefwiggum init` | Initialize the database |
| `chiefwiggum reset` | Reset the database |

## Library Usage

```python
import asyncio
from chiefwiggum import (
    init_db,
    register_ralph_instance,
    claim_task,
    complete_task,
    sync_tasks_from_fix_plan,
)

async def main():
    # Initialize database
    await init_db()

    # Register this instance
    ralph_id = await register_ralph_instance("my-ralph", project="tian")

    # Sync tasks from fix plan
    await sync_tasks_from_fix_plan("@fix_plan.md", project="tian")

    # Claim next available task
    task = await claim_task(ralph_id, project="tian")
    if task:
        print(f"Claimed: {task['task_title']}")

        # Do work...

        # Complete the task
        await complete_task(ralph_id, task['task_id'], message="Done!")

asyncio.run(main())
```

## Architecture

```
~/.chiefwiggum/
  coordination.db     # SQLite database

chiefwiggum/
  __init__.py         # Public API
  coordination.py     # Core logic
  models.py           # Pydantic models
  database.py         # SQLite schema
  cli.py              # CLI commands
  tui.py              # Rich dashboard
```

## Database

The database is stored at `~/.chiefwiggum/coordination.db` by default. You can override this with the `CHIEFWIGGUM_DB` environment variable.

### Tables

- **task_claims**: Tasks parsed from fix plans with claim status
- **ralph_instances**: Registered Ralph instances with heartbeat

## Task Priority Order

Tasks are claimed in priority order:

1. **HIGH** - Critical path items
2. **MEDIUM** - Important but not blocking
3. **LOWER** - Nice to have
4. **POLISH** - Cleanup and polish

## Claim Expiry

Claims automatically expire after 7 minutes if not extended. This prevents tasks from being locked by crashed instances.

Instances are marked as crashed after 10 minutes without a heartbeat, and their claims are released.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CHIEFWIGGUM_DB` | Path to SQLite database | `~/.chiefwiggum/coordination.db` |
