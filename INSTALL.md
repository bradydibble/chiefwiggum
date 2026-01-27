# ChiefWiggum Installation Guide

## Quick Start

### For Development (Recommended)
```bash
git clone https://github.com/bradydibble/chiefwiggum.git
cd chiefwiggum
make dev-setup
```

### For Production Use
```bash
pipx install git+https://github.com/bradydibble/chiefwiggum.git
```

## Installation Methods

### Method 1: Editable Install (Development)

**Use when:** You're developing ChiefWiggum or want to pull updates frequently.

**Advantages:**
- Code changes take effect immediately (no reinstall needed for most changes)
- Easy to debug and modify
- Git pull + restart is usually enough

**Installation:**
```bash
# Clone repository
git clone https://github.com/bradydibble/chiefwiggum.git
cd chiefwiggum

# Option A: Using Make (recommended)
make install-dev

# Option B: Using pip directly
pip install -e ".[dev]"

# Verify installation
make verify
# or
chiefwiggum verify
```

**When to Reinstall:**
You need to reinstall when:
- Dependencies change in `pyproject.toml`
- Entry points change (CLI commands)
- New modules are added to package
- Shell scripts in `chiefwiggum/scripts/` are modified

**Quick Reinstall:**
```bash
make reinstall
```

### Method 2: pipx Install (Production)

**Use when:** You want isolated ChiefWiggum installation without affecting other Python packages.

**Installation:**
```bash
pipx install git+https://github.com/bradydibble/chiefwiggum.git
```

**Update:**
```bash
pipx upgrade chiefwiggum
# or force reinstall
make reinstall-pipx
```

### Method 3: From PyPI (Stable Releases)

**Use when:** You want the latest stable release.

**Installation:**
```bash
pipx install chiefwiggum
```

## Verification

After installation, verify everything works:

```bash
# Check version
chiefwiggum --version

# Run verification
chiefwiggum verify

# Or with make
make verify
```

## Common Issues

### "Command not found: chiefwiggum"

**Solution:**
```bash
# Check if pipx bin directory is in PATH
pipx ensurepath

# Or reinstall
make reinstall
```

### "Module not found" errors

**Solution:**
```bash
# Reinstall with dev dependencies
make reinstall
```

### Changes not taking effect (editable install)

**Reasons:**
1. Changed `pyproject.toml` dependencies → Need reinstall
2. Changed shell scripts → Need reinstall
3. Added new modules → Need restart (not reinstall)
4. Changed Python code → Just restart

**Quick fix:**
```bash
make reinstall
```

## Development Workflow

### Daily Development

1. **Make code changes** - Edit Python files
2. **Restart processes** - Stop/start TUI or Ralphs
3. **Test changes** - Run `make test`

### After Pulling Updates

**Option A: One command (recommended)**
```bash
wig update      # Does git pull + reinstall + verify
```

**Option B: Manual steps**
```bash
git pull
make reinstall  # Safe to run every time
make verify     # Confirm everything works
```

### Before Committing

```bash
make lint       # Check code style
make test       # Run all tests
```

## Makefile Commands

Run `make help` to see all available commands:

- `make install` - Editable install
- `make install-dev` - Editable install with dev dependencies
- `make reinstall` - Quick reinstall (useful after updates)
- `make test` - Run test suite
- `make verify` - Verify installation
- `make clean` - Clean build artifacts
- `make lint` - Run linting checks
- `make format` - Format code with ruff
- `make build` - Build distribution packages

## Requirements

- Python 3.11+
- Git
- Claude CLI (for Ralph functionality)
- pipx (for isolated installs) or virtual environment for development

### Python Environment Setup

Modern Python installations (macOS with Homebrew, newer Linux distributions) use PEP 668 externally-managed environments, which means you need to use either:

**Option A: pipx (Recommended for users)**
```bash
brew install pipx  # macOS
pipx ensurepath
```

**Option B: Virtual environment (Recommended for developers)**
```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate  # Windows

# Now all pip/make commands will work
make dev-setup
```

Once in a virtual environment, all `make` commands will work without issues.

## Support

If you encounter issues:
1. Run `chiefwiggum verify` to diagnose
2. Check logs in `~/.local/share/chiefwiggum/`
3. Try `make reinstall`
4. Report issue with verification output at https://github.com/bradydibble/chiefwiggum/issues
