.PHONY: install install-dev reinstall reinstall-pipx test test-fast lint format build clean verify dev-setup help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install in editable mode (recommended for development)
	python3 -m pip install -e .

install-dev:  ## Install with dev dependencies (includes pytest)
	python3 -m pip install -e ".[dev]"

reinstall:  ## Quick reinstall (useful after pulling changes)
	python3 -m pip uninstall -y chiefwiggum || true
	python3 -m pip install -e ".[dev]"

reinstall-pipx:  ## Reinstall via pipx (for production use)
	pipx uninstall chiefwiggum || true
	pipx install .

test:  ## Run all tests
	pytest tests/ -v

test-fast:  ## Run tests without slow integration tests
	pytest tests/ -v -m "not slow"

lint:  ## Run linting checks
	ruff check chiefwiggum tests

format:  ## Format code with ruff
	ruff format chiefwiggum tests

build:  ## Build distribution packages
	python -m build

clean:  ## Clean build artifacts
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

verify:  ## Verify installation is working
	@echo "Checking ChiefWiggum installation..."
	@chiefwiggum --version || (echo "❌ CLI not found" && exit 1)
	@python -c "import chiefwiggum; print(f'✅ Version: {chiefwiggum.__version__}')" || (echo "❌ Import failed" && exit 1)
	@echo "✅ Installation verified!"

dev-setup:  ## Complete development setup (recommended for new clones)
	@echo "Setting up ChiefWiggum development environment..."
	python3 -m pip install --upgrade pip
	python3 -m pip install -e ".[dev]"
	@echo "✅ Development setup complete!"
	@$(MAKE) verify
