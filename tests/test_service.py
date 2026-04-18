"""Tests for the wig service launchd integration.

These do NOT actually invoke `launchctl` against the user's real system.
They cover: template rendering, install path computation, platform guard,
and the structure of the rendered plist.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from chiefwiggum import service


class TestPlatformGuard:
    def test_is_supported_matches_darwin(self):
        assert service.is_supported() == (sys.platform == "darwin")

    @pytest.mark.skipif(sys.platform == "darwin", reason="macOS-only")
    def test_install_on_non_macos_raises_via_require(self):
        with pytest.raises(RuntimeError) as excinfo:
            service._require_macos()
        assert "macOS" in str(excinfo.value)


class TestPlistRendering:
    def test_template_has_all_placeholders(self):
        template = service._read_plist_template()
        for placeholder in ("{LABEL}", "{CHIEFWIGGUM_BIN}", "{STDOUT_LOG}", "{STDERR_LOG}", "{HOME}", "{PATH}"):
            assert placeholder in template, f"template missing {placeholder}"

    def test_render_fills_placeholders_and_preserves_structure(self, tmp_path):
        fake_bin = tmp_path / "bin" / "chiefwiggum"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python\n")
        fake_state = tmp_path / "state"
        fake_state.mkdir()

        rendered = service._render_plist(fake_bin, fake_state)

        # No placeholders should remain.
        for placeholder in ("{LABEL}", "{CHIEFWIGGUM_BIN}", "{STDOUT_LOG}", "{STDERR_LOG}", "{HOME}", "{PATH}"):
            assert placeholder not in rendered, f"unrendered placeholder {placeholder}"

        # Key values substituted.
        assert service.LABEL in rendered
        assert str(fake_bin) in rendered
        assert str(fake_state / "daemon.stdout.log") in rendered
        assert str(fake_state / "daemon.stderr.log") in rendered
        assert str(Path.home()) in rendered

        # PATH entry includes the binary's dir.
        assert str(fake_bin.parent) in rendered

        # Structural sanity — has the KeepAlive=true and RunAtLoad=true blocks.
        assert "<key>KeepAlive</key>" in rendered
        assert "<key>RunAtLoad</key>" in rendered
        # Both should be set to true (not false).
        assert "<false/>" not in rendered.split("<key>KeepAlive</key>", 1)[1].split("</key>", 2)[0]

    def test_plist_install_path_points_to_launch_agents(self):
        path = service.plist_install_path()
        assert path.parent == Path.home() / "Library" / "LaunchAgents"
        assert path.name == f"{service.LABEL}.plist"


class TestInstallUninstallIdempotency:
    @pytest.mark.skipif(not service.is_supported(), reason="macOS-only")
    def test_install_without_binary_returns_failure_without_writing_plist(self, tmp_path, monkeypatch):
        # Point LaunchAgents at tmp so we don't pollute the user's real dir.
        monkeypatch.setattr(
            service,
            "launch_agents_dir",
            lambda: tmp_path / "LaunchAgents",
        )
        monkeypatch.setattr(
            service,
            "plist_install_path",
            lambda: tmp_path / "LaunchAgents" / f"{service.LABEL}.plist",
        )
        # Force find_chiefwiggum_bin to return None.
        with patch.object(service, "find_chiefwiggum_bin", return_value=None):
            result = service.install()

        assert not result.installed
        assert "Could not find" in result.message
        assert not (tmp_path / "LaunchAgents" / f"{service.LABEL}.plist").exists()

    @pytest.mark.skipif(not service.is_supported(), reason="macOS-only")
    def test_uninstall_with_no_plist_is_nop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            service,
            "plist_install_path",
            lambda: tmp_path / "LaunchAgents" / f"{service.LABEL}.plist",
        )
        # Stub out launchctl so we don't actually talk to the OS.
        import subprocess as sp
        monkeypatch.setattr(
            sp,
            "run",
            lambda *a, **kw: sp.CompletedProcess(args=a, returncode=0, stdout="", stderr=""),
        )
        result = service.uninstall()
        assert not result.installed
        assert "nothing to uninstall" in result.message
