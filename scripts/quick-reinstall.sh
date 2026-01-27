#!/usr/bin/env bash
# Quick reinstall script for developers

set -e

echo "🔄 Reinstalling ChiefWiggum..."

# Uninstall (ignore errors if not installed)
python3 -m pip uninstall -y chiefwiggum 2>/dev/null || true

# Reinstall in editable mode with dev dependencies
python3 -m pip install -e ".[dev]"

# Verify
python -c "import chiefwiggum; print(f'✅ ChiefWiggum {chiefwiggum.__version__} reinstalled')"

echo "✅ Reinstall complete!"
