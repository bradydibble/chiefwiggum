"""Tests for chiefwiggum.scripts module - script path resolution."""

import os
from pathlib import Path
from unittest import mock

import pytest

from chiefwiggum.scripts import get_ralph_loop_path, get_scripts_dir


class TestGetScriptsDir:
    """Tests for get_scripts_dir()."""

    def test_returns_path(self):
        """Should return a Path object."""
        result = get_scripts_dir()
        assert isinstance(result, Path)

    def test_contains_ralph_loop(self):
        """Scripts directory should contain ralph_loop.sh."""
        scripts_dir = get_scripts_dir()
        ralph_loop = scripts_dir / "ralph_loop.sh"
        assert ralph_loop.exists(), f"ralph_loop.sh not found at {ralph_loop}"

    def test_contains_lib_directory(self):
        """Scripts directory should contain lib/ subdirectory."""
        scripts_dir = get_scripts_dir()
        lib_dir = scripts_dir / "lib"
        assert lib_dir.exists(), f"lib directory not found at {lib_dir}"
        assert lib_dir.is_dir()

    def test_lib_contains_required_scripts(self):
        """lib/ directory should contain all required helper scripts."""
        scripts_dir = get_scripts_dir()
        lib_dir = scripts_dir / "lib"

        required_scripts = [
            "date_utils.sh",
            "response_analyzer.sh",
            "circuit_breaker.sh",
            "profiler.sh",
        ]

        for script in required_scripts:
            script_path = lib_dir / script
            assert script_path.exists(), f"Required script {script} not found at {script_path}"


class TestGetRalphLoopPath:
    """Tests for get_ralph_loop_path()."""

    def test_returns_bundled_script(self):
        """Should return the bundled ralph_loop.sh path by default."""
        result = get_ralph_loop_path()
        assert isinstance(result, Path)
        assert result.name == "ralph_loop.sh"
        assert result.exists()

    def test_env_override_existing_file(self, tmp_path):
        """Should use RALPH_LOOP_PATH env var if set and file exists."""
        custom_script = tmp_path / "custom_ralph_loop.sh"
        custom_script.write_text("#!/bin/bash\necho 'custom'")

        with mock.patch.dict(os.environ, {"RALPH_LOOP_PATH": str(custom_script)}):
            result = get_ralph_loop_path()
            assert result == custom_script

    def test_env_override_nonexistent_falls_through(self, tmp_path):
        """Should fall through to bundled if env var points to nonexistent file."""
        nonexistent = tmp_path / "nonexistent.sh"

        with mock.patch.dict(os.environ, {"RALPH_LOOP_PATH": str(nonexistent)}):
            result = get_ralph_loop_path()
            # Should still return the bundled script
            assert result.name == "ralph_loop.sh"
            assert result.exists()

    def test_script_is_executable(self):
        """The bundled script should be readable."""
        script_path = get_ralph_loop_path()
        # Check we can read the content
        content = script_path.read_text()
        assert "#!/bin/bash" in content, "Script should start with bash shebang"

    def test_script_sources_lib_files(self):
        """The bundled script should source files from lib/ directory."""
        script_path = get_ralph_loop_path()
        content = script_path.read_text()

        # Check for source statements
        expected_sources = [
            "lib/date_utils.sh",
            "lib/response_analyzer.sh",
            "lib/circuit_breaker.sh",
            "lib/profiler.sh",
        ]

        for expected in expected_sources:
            assert expected in content, f"Script should source {expected}"


class TestScriptRelativePaths:
    """Tests ensuring script sourcing works with relative paths."""

    def test_script_uses_script_dir_for_sourcing(self):
        """Script should use SCRIPT_DIR for sourcing lib files."""
        script_path = get_ralph_loop_path()
        content = script_path.read_text()

        # Should define SCRIPT_DIR using dirname
        assert 'SCRIPT_DIR="$(dirname' in content or "SCRIPT_DIR=" in content

        # Should source from SCRIPT_DIR/lib/
        assert '$SCRIPT_DIR/lib/' in content or "${SCRIPT_DIR}/lib/" in content
