# ChiefWiggum Quick Start Guide

## For New Developers

### First Time Setup

```bash
# 1. Clone the repository
git clone https://github.com/bradydibble/chiefwiggum.git
cd chiefwiggum

# 2. Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows

# 3. One-command setup
make dev-setup

# 4. Verify everything works
make verify
```

### After Pulling Updates

```bash
# One command does it all (git pull + reinstall + verify)
wig update

# Or do it manually:
make reinstall
make verify
```

## Common Commands

```bash
make help          # Show all available commands
make test          # Run full test suite
make test-fast     # Quick tests (skip slow integration tests)
make lint          # Check code style
make format        # Auto-format code
make clean         # Clean build artifacts
make build         # Build distribution packages
```

## Without Make

If you prefer not to use Make:

```bash
# Install
python3 -m pip install -e ".[dev]"

# Reinstall
python3 -m pip uninstall -y chiefwiggum
python3 -m pip install -e ".[dev]"

# Verify
chiefwiggum verify

# Test
pytest tests/ -v
```

## Troubleshooting

### "externally-managed-environment" Error

Modern Python requires virtual environments:

```bash
python3 -m venv venv
source venv/bin/activate
make dev-setup
```

### "Command not found: chiefwiggum"

```bash
# Check installation
python3 -c "import chiefwiggum; print(chiefwiggum.__version__)"

# Reinstall
make reinstall
```

### "make: command not found"

Make is not required! Use the scripts directly:

```bash
./scripts/dev-setup.sh
./scripts/quick-reinstall.sh
```

Or use pip commands directly (see "Without Make" section above).

## For More Information

- **Installation Guide**: See [INSTALL.md](INSTALL.md) for detailed instructions
- **Project README**: See [README.md](README.md) for usage and features
- **Implementation Details**: See [PACKAGING_IMPLEMENTATION_SUMMARY.md](PACKAGING_IMPLEMENTATION_SUMMARY.md)
