#!/usr/bin/env bash
# Development environment setup script

set -e

echo "🔧 Setting up ChiefWiggum development environment..."

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
required_version="3.11"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    echo "❌ Python 3.11+ required. Found: $python_version"
    exit 1
fi

echo "✅ Python version: $python_version"

# Upgrade pip
echo "📦 Upgrading pip..."
python3 -m pip install --upgrade pip

# Install in editable mode with dev dependencies
echo "📦 Installing ChiefWiggum in editable mode..."
python3 -m pip install -e ".[dev]"

# Verify installation
echo "🔍 Verifying installation..."
python -c "import chiefwiggum; print(f'✅ ChiefWiggum {chiefwiggum.__version__} installed')"

# Check for Claude CLI
if command -v claude &> /dev/null; then
    echo "✅ Claude CLI found: $(claude --version 2>&1 | head -n1)"
else
    echo "⚠️  Claude CLI not found. Install from: https://docs.anthropic.com/claude/docs/cli"
fi

# Initialize database
echo "🗄️  Initializing database..."
python -c "import asyncio; from chiefwiggum import init_db; asyncio.run(init_db())"

echo ""
echo "✅ Development environment ready!"
echo ""
echo "Next steps:"
echo "  • Run 'chiefwiggum verify' to check installation"
echo "  • Run 'chiefwiggum tui' to start the TUI"
echo "  • Run 'make help' to see all available commands"
