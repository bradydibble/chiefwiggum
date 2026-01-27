# ChiefWiggum

**Run 2-5 Claude Code instances in parallel on your codebase.** ChiefWiggum prevents duplicate work, manages task claiming, and provides a live TUI dashboard showing exactly what each Ralph (Claude Code instance) is doing in real-time.

## What It Does

ChiefWiggum transforms your task list into a distributed work queue:

1. **Parse your task list** - Reads any markdown file with hierarchical tasks (your fix plan, backlog, TODO.md, etc.)
2. **Coordinate multiple Ralphs** - Spawn 2-5 Claude Code instances that claim tasks automatically
3. **Prevent conflicts** - Task claiming ensures no two Ralphs work on the same thing
4. **Live monitoring** - Rich TUI dashboard shows real-time progress, logs, and system stats
5. **Auto-recovery** - Crashed instances release their claims; failed tasks go to retry queue

**Perfect for:** Large refactors, bug marathons, feature implementations with many independent tasks, or any time you want to parallelize AI-assisted development.

## The TUI Dashboard

Launch `chiefwiggum tui` to get a live dashboard with:

- **Ralph Instances Panel** - See all running Claude Code instances, their status, current task, and loop count
- **Tasks Panel** - View pending, in-progress, completed, and failed tasks with priorities
- **Live Stats** - Total tasks, completion rate, active instances, system resources
- **Task History** - Audit log of all task state changes with timestamps
- **Spawn Control** - Press `n` to spawn new Ralph instances with custom config
- **Log Streaming** - Press `v` to tail logs from any Ralph in real-time
- **Error Tracking** - Press `e` to see recent errors and debugging info
- **Settings** - Press `S` to configure API keys, permissions, models, auto-scaling

**Keyboard shortcuts:** `n`=spawn, `s`=stop, `p`=pause/resume, `/`=search, `d`=task details, `H`=history, `q`=quit

## Installation

### Quick Start (Development)
```bash
git clone https://github.com/bradydibble/chiefwiggum.git
cd chiefwiggum
make dev-setup
```

### Production Install
```bash
pipx install git+https://github.com/bradydibble/chiefwiggum.git
```

📖 **See [INSTALL.md](INSTALL.md) for detailed installation options and troubleshooting.**

## Verification

After installation, verify everything works:
```bash
chiefwiggum verify
# or
make verify
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

### Typical Workflow

1. **Navigate to your project directory** containing your task list:
   ```bash
   cd ~/projects/myproject
   ```

2. **Launch the TUI dashboard** (this will auto-initialize the database):
   ```bash
   chiefwiggum tui
   ```

3. **Press `y` to sync** your task list (first time only):
   - The TUI will discover `@fix_plan.md` or any markdown file with tasks
   - Or manually sync: `chiefwiggum sync your-tasks.md --project myproject`

4. **Press `n` to spawn Ralph instances** (start with 2-3):
   - Choose a model (Sonnet, Opus, Haiku)
   - Set permissions and targeting
   - Watch them claim and work on tasks automatically

5. **Monitor progress** in real-time:
   - See which Ralph is working on what
   - Check completion status
   - View logs with `v`, errors with `e`
   - Press `H` for full task history

### Alternative: CLI-Only Workflow

If you prefer the command line:

```bash
cd ~/projects/myproject

# Initialize and sync tasks
chiefwiggum init
chiefwiggum sync @fix_plan.md --project myproject

# Register this terminal as a Ralph instance
chiefwiggum register --name ralph-1 --project myproject

# Claim and work on next task
chiefwiggum claim myproject --ralph-id $(hostname)-ralph-1

# View status
chiefwiggum status
```

## Task List Format

ChiefWiggum can parse **any markdown file** with hierarchical tasks. The file doesn't have to be named `@fix_plan.md` - use whatever name you want (`TODO.md`, `backlog.md`, `tasks.md`, etc.).

### Supported Formats

ChiefWiggum understands multiple task list formats:

#### Format 1: Numbered Tasks
```markdown
## HIGH PRIORITY - Authentication System

### 1. Add login endpoint
- [ ] Create POST /auth/login route
- [ ] Add JWT token generation
- [x] Write tests

### 2. Add logout functionality COMPLETE
- [x] Invalidate tokens
- [x] Clear session data
```

#### Format 2: ID-Based Tasks (Jira-style)
```markdown
## PRODUCT FEEDBACK (IMMEDIATE PRIORITY)

#### PF-1: Fix timezone display
- [ ] Change server timezone to Pacific
- [ ] Update all date formatters

#### BUG-42: Login redirects incorrectly
- [ ] Fix redirect logic after OAuth
```

#### Format 3: Tier-Based Sections
```markdown
### Tier 1 - Critical Path

#### Database Migration System
- [ ] Design migration schema
- [ ] Implement up/down migrations
- [ ] Add CLI commands
```

### Priority Detection

ChiefWiggum automatically detects priority from section headers:

- **HIGH**: "HIGH PRIORITY", "IMMEDIATE PRIORITY", "CRITICAL", "Tier 1"
- **MEDIUM**: "MEDIUM PRIORITY", "Tier 2"
- **LOWER**: "LOWER PRIORITY", "Tier 3"
- **POLISH**: "POLISH", "Tier 4"

Tasks are claimed in priority order (HIGH → MEDIUM → LOWER → POLISH).

### Completion Markers

Mark tasks complete in your markdown file:
- Add `COMPLETE` to the task title: `### 5. Login System COMPLETE`
- Add a checkmark: `### 5. Login System ✅`
- Check all subtasks: If all `- [x]` subtasks are checked, task is auto-marked complete

ChiefWiggum syncs these markers back to your file when tasks complete.

## CLI Commands

### Core Commands

| Command | Description | Example |
|---------|-------------|---------|
| `chiefwiggum tui` | Launch live TUI dashboard | `chiefwiggum tui` |
| `chiefwiggum status` | Show active instances and tasks | `chiefwiggum status --project myproject` |
| `chiefwiggum sync <file>` | Sync tasks from markdown file | `chiefwiggum sync tasks.md --project myapp` |
| `chiefwiggum init` | Initialize coordination database | `chiefwiggum init` |

### Instance Management

| Command | Description | Example |
|---------|-------------|---------|
| `chiefwiggum register --name <id>` | Register this terminal as Ralph instance | `chiefwiggum register --name worker-1` |
| `chiefwiggum shutdown --ralph-id <id>` | Shutdown a specific Ralph | `chiefwiggum shutdown --ralph-id worker-1` |
| `chiefwiggum list-instances` | List all instances (active + stopped) | `chiefwiggum list-instances` |

### Task Management

| Command | Description | Example |
|---------|-------------|---------|
| `chiefwiggum claim <project>` | Claim next available task | `chiefwiggum claim myproject --ralph-id worker-1` |
| `chiefwiggum complete <task-id>` | Mark task complete | `chiefwiggum complete task-1-fix-auth` |
| `chiefwiggum release <task-id>` | Release task claim | `chiefwiggum release task-1-fix-auth` |
| `chiefwiggum list` | List all tasks with IDs | `chiefwiggum list --project myproject` |

### Utility Commands

| Command | Description | Example |
|---------|-------------|---------|
| `chiefwiggum update` | Update to latest version | `wig update` |
| `chiefwiggum verify` | Verify installation | `wig verify` |
| `chiefwiggum export-history` | Export task history to CSV | `chiefwiggum export-history --project myproject` |
| `chiefwiggum paths` | Show database and config paths | `chiefwiggum paths` |
| `chiefwiggum reset` | Reset database (⚠️ deletes all data) | `chiefwiggum reset` |
| `chiefwiggum --version` | Show version info | `chiefwiggum --version` |

## How It Works

### Task Claiming & Priority

When a Ralph instance needs work:
1. Queries for unclaimed tasks in priority order (HIGH → MEDIUM → LOWER → POLISH)
2. Claims the task (marks as `in_progress`, records Ralph ID, sets claim expiry)
3. Works on the task (running Claude Code with the task description)
4. Completes or fails the task (updates status, releases claim)

### Auto-Recovery

- **Claim expiry**: Claims auto-expire after 7 minutes if not extended (prevents deadlocks)
- **Heartbeat monitoring**: Instances marked as crashed after 10 minutes without heartbeat
- **Failed task retry**: Failed tasks go to retry queue, can be re-attempted
- **Crash cleanup**: Crashed instances automatically release their claims

### Project Structure

ChiefWiggum uses a **project-based model**. Each project has:
- A task list file (your markdown file with tasks)
- A project name (defaults to parent directory name)
- Multiple Ralph instances working on that project
- Shared coordination database at `~/.chiefwiggum/coordination.db`

**Best practice**: Run `chiefwiggum tui` in your project directory. The TUI will auto-discover your task file and set the project name appropriately.

## Library/API Usage

You can also use ChiefWiggum as a Python library:

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

    # Sync tasks from any markdown file
    await sync_tasks_from_fix_plan("path/to/tasks.md", project="myproject")

    # Claim next available task
    task = await claim_task(ralph_id, project="myproject")
    if task:
        print(f"Claimed: {task['task_title']}")

        # Do your work here...
        # (Normally this would be Claude Code processing)

        # Mark complete
        await complete_task(ralph_id, task['task_id'], message="Done!")

asyncio.run(main())
```

## Configuration

### Database Location

Database: `~/.chiefwiggum/coordination.db` (SQLite)

Override with environment variable:
```bash
export CHIEFWIGGUM_DB="/custom/path/coordination.db"
```

### Configuration File

Config: `~/.chiefwiggum/config.yaml`

```yaml
anthropic_api_key: sk-ant-...
max_ralphs: 5
default_model: sonnet
auto_update_fix_plan: true
ralph_loop_settings:
  show_tokens: true
  context_expansion: true
```

All settings can be configured via the TUI (Press `S`).

## Tips & Best Practices

### Starting Your First Multi-Ralph Session

1. **Start small**: Begin with 2 Ralphs, not 5. Watch how they coordinate.
2. **Use the TUI**: The dashboard is the best way to understand what's happening.
3. **Check logs**: Press `v` in the TUI to watch Ralph logs in real-time.
4. **Set targeting**: Use targeting config to assign specific Ralphs to specific task categories.
5. **Monitor token usage**: Enable `show_tokens` in ralph_loop settings to track costs.

### Alias for Quick Access

Add to your `~/.bashrc` or `~/.zshrc`:
```bash
alias wig='chiefwiggum tui'
```

Then just run `wig` in any project directory to launch the dashboard!

### Recommended Task List Structure

For best results, structure your markdown file like this:
```markdown
## HIGH PRIORITY - Core Functionality
### 1. Critical bug fixes
### 2. Essential features

## MEDIUM PRIORITY - Improvements
### 3. Performance optimizations
### 4. Better error handling

## LOWER PRIORITY - Polish
### 5. UI improvements
### 6. Documentation updates
```

### When to Use Multiple Ralphs

- ✅ **Good**: Large refactors with many independent files
- ✅ **Good**: Bug fixes across unrelated modules
- ✅ **Good**: Test writing for multiple components
- ⚠️ **Careful**: Tasks that touch the same files (may cause git conflicts)
- ❌ **Avoid**: Tasks that depend on each other sequentially

## Troubleshooting

### Installation Issues

**Python version too old**
```bash
# macOS - install newer Python
brew install python@3.11
python3 --version  # Verify
```

**pipx not found**
```bash
brew install pipx
pipx ensurepath
# Restart terminal
```

### Runtime Issues

**"ANTHROPIC_API_KEY not set"**
- TUI: Press `S` → Enter API key
- CLI: `export ANTHROPIC_API_KEY='sk-ant-...'`

**"No task file found"**
- Make sure you're in a directory with a markdown file containing tasks
- Or specify path: `chiefwiggum sync /path/to/tasks.md`

**Ralphs not claiming tasks**
- Check they're spawned: `chiefwiggum status`
- View logs: Press `v` in TUI
- Check for errors: Press `e` in TUI

**Tasks stuck "in_progress"**
- Claims auto-expire after 7 minutes
- Or manually release: `chiefwiggum release <task-id>`
- Check if Ralph crashed: Look for crashed instances in TUI

**Database issues**
- View location: `chiefwiggum paths`
- Reset: `chiefwiggum reset` (⚠️ deletes all data)
- Backup first: `cp ~/.chiefwiggum/coordination.db ~/backup.db`

### Getting Help

File issues at https://github.com/bradydibble/chiefwiggum/issues

## Development

### Quick Update (after pulling changes)
```bash
wig update              # Smart update (git pull + reinstall)
# or
make reinstall          # Manual reinstall
```

### Run Tests
```bash
make test
```

### Other Development Commands
```bash
make help       # Show all available commands
make lint       # Run linting checks
make format     # Format code with ruff
make build      # Build distribution packages
```

📖 **See [INSTALL.md](INSTALL.md) for complete development workflow.**

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CHIEFWIGGUM_DB` | Path to SQLite database | `~/.chiefwiggum/coordination.db` |
