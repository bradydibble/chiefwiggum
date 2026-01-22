# ChiefWiggum

Multi-Ralph Coordination System for Claude Code instances.

ChiefWiggum orchestrates multiple Ralph (Claude Code) instances working on the same codebase, preventing duplicate work, managing task claiming, and providing visibility into what each Ralph is doing.

## Installation

```bash
pipx install git+https://github.com/bradydibble/chiefwiggum.git
```

Or for development:
```bash
git clone https://github.com/bradydibble/chiefwiggum.git
cd chiefwiggum
pip install -e .
```

## Prerequisites

Before installing ChiefWiggum, ensure you have:

- **Python 3.11 or higher** - Check with `python3 --version`
- **pipx** (for isolated installation) - Install with `brew install pipx` on macOS
- **Claude Code CLI** - The `claude` command must be available in your PATH
- **Anthropic API key** - Get from https://console.anthropic.com/

### Setting up your API Key

ChiefWiggum needs your Anthropic API key to coordinate Claude instances.

**Option 1: Environment Variable (Recommended)**
```bash
export ANTHROPIC_API_KEY='your-api-key-here'
# Add to ~/.bashrc or ~/.zshrc to persist
```

**Option 2: Configuration File**
ChiefWiggum will create `~/.chiefwiggum/config.yaml` on first run where you can add:
```yaml
anthropic_api_key: your-api-key-here
```

**Option 3: Via TUI Settings**
```bash
chiefwiggum tui
# Press 'S' for Settings, then enter your API key
```

## Quick Start

### 1. Initialize the database

```bash
chiefwiggum init
```

### 2. Sync tasks from a fix plan

```bash
chiefwiggum sync ~/projects/myproject/@fix_plan.md --project myproject
```

### 3. Register a Ralph instance

```bash
chiefwiggum register --name ralph-1 --project myproject
```

### 4. Claim a task

```bash
chiefwiggum claim myproject --ralph-id arch-ralph-1
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
    ralph_id = await register_ralph_instance("my-ralph", project="myproject")

    # Sync tasks from fix plan
    await sync_tasks_from_fix_plan("@fix_plan.md", project="myproject")

    # Claim next available task
    task = await claim_task(ralph_id, project="myproject")
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

## Troubleshooting

### Installation Issues

**Python version too old**
```bash
# macOS - install newer Python
brew install python@3.11
# Verify
python3 --version
```

**pipx not found**
```bash
# macOS
brew install pipx
pipx ensurepath
# Restart your terminal
```

### Runtime Issues

**"ANTHROPIC_API_KEY not set"**
- Set via environment: `export ANTHROPIC_API_KEY='your-key'`
- Or configure via TUI: `chiefwiggum tui` → Press 'S'

**"No active Ralph instances"**
- Register a Ralph first: `chiefwiggum register --name my-ralph --project myproject`
- Or spawn via TUI: `chiefwiggum tui` → Press 'n'

**"Task stays in pending status"**
- Ensure Ralph instances are actually running and claiming tasks
- Check instance status: `chiefwiggum status`
- View detailed status: `chiefwiggum tui`

**Database corruption or errors**
- Reset database: `chiefwiggum reset` (Warning: deletes all data)
- View database location: `chiefwiggum paths`

**macOS permission errors**
- Ensure terminal has Full Disk Access (System Settings → Privacy & Security)
- Database directory `~/.chiefwiggum/` must be writable

### Getting Help

For internal support, contact the ChiefWiggum maintainer or file an issue in the repository.

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
