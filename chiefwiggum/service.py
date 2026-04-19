"""ChiefWiggum service integration (macOS launchd).

Makes the chiefwiggum daemon auto-start on login and auto-restart on crash
via launchd's `KeepAlive=true` semantics. Install once with
`wig service install`, and the OS takes it from there — no more dependency
on a terminal window being open.

On non-macOS platforms the commands emit a helpful error pointing to the
systemd-unit equivalent (future work).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

LABEL = "com.chiefwiggum.daemon"


@dataclass
class ServiceInstallResult:
    installed: bool
    plist_path: Path
    message: str


def is_supported() -> bool:
    return sys.platform == "darwin"


def _require_macos() -> None:
    if not is_supported():
        raise RuntimeError(
            "wig service commands currently only support macOS (launchd). "
            "On Linux, run the daemon under systemd user-service; template "
            "support is planned — for now, `wig daemon start` is the manual "
            "equivalent."
        )


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_install_path() -> Path:
    return launch_agents_dir() / f"{LABEL}.plist"


def _read_plist_template() -> str:
    """Load the packaged plist template as text."""
    with resources.as_file(
        resources.files("chiefwiggum.resources").joinpath(f"{LABEL}.plist")
    ) as template_path:
        return Path(template_path).read_text()


def _render_plist(chiefwiggum_bin: Path, state_dir: Path) -> str:
    template = _read_plist_template()
    home = Path.home()
    # Give launchd a sane PATH. launchd-spawned daemons don't inherit a
    # login shell, so PATH defaults to a minimal /usr/bin:/bin. We build
    # a superset that covers the places the daemon + its ralph workers
    # actually need to find binaries (claude, node, npm, git, jq, python).
    #
    # The specific shim directories are preferred over system paths so
    # that pyenv/nvm versions the user chose interactively match what
    # the daemon runs — otherwise you get "works in terminal, fails in
    # daemon" confusion.
    import glob as _glob
    nvm_bin_globs = sorted(
        _glob.glob(str(home / ".nvm" / "versions" / "node" / "*" / "bin"))
    )
    # Latest nvm version last → wins in PATH ordering below (appears first).
    nvm_bin_dirs = list(reversed(nvm_bin_globs))

    path_entries = [
        str(chiefwiggum_bin.parent),
        *nvm_bin_dirs,
        str(home / ".pyenv" / "shims"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    # De-dup while preserving order.
    seen: set[str] = set()
    deduped = []
    for entry in path_entries:
        if entry and entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    launch_path = ":".join(deduped)

    replacements = {
        "{LABEL}": LABEL,
        "{CHIEFWIGGUM_BIN}": str(chiefwiggum_bin),
        "{STDOUT_LOG}": str(state_dir / "daemon.stdout.log"),
        "{STDERR_LOG}": str(state_dir / "daemon.stderr.log"),
        "{HOME}": str(home),
        "{PATH}": launch_path,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def find_chiefwiggum_bin() -> Path | None:
    """Return the path to the `chiefwiggum`/`wig` entry point on PATH, or None.

    Prefers `chiefwiggum` over `wig` so the plist survives a user removing
    the short alias.
    """
    for candidate in ("chiefwiggum", "wig"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    return None


def install(chiefwiggum_bin: Path | None = None) -> ServiceInstallResult:
    """Render and install the launchd plist, then load it.

    Idempotent: if the plist already exists, it is overwritten; if the agent
    is already loaded, it is reloaded with the new contents.
    """
    _require_macos()
    from chiefwiggum.paths import get_paths

    bin_path = chiefwiggum_bin or find_chiefwiggum_bin()
    if bin_path is None:
        return ServiceInstallResult(
            installed=False,
            plist_path=plist_install_path(),
            message=(
                "Could not find `chiefwiggum` or `wig` on PATH. "
                "Install chiefwiggum (e.g. `pipx install chiefwiggum` or "
                "`pip install -e .`) and retry."
            ),
        )

    paths = get_paths()
    paths.ensure_dirs()
    state_dir = paths.state_dir

    plist_path = plist_install_path()
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    rendered = _render_plist(bin_path, state_dir)
    plist_path.write_text(rendered)

    # Reload: launchctl unload (best-effort; may fail if not already loaded)
    # then launchctl load. Use bootstrap/bootout on newer macOS, fall back to
    # load/unload on older.
    _launchctl_reload(plist_path)

    return ServiceInstallResult(
        installed=True,
        plist_path=plist_path,
        message=f"Installed and loaded {LABEL}",
    )


def uninstall() -> ServiceInstallResult:
    """Unload the launchd agent and remove the plist file."""
    _require_macos()

    plist_path = plist_install_path()
    if not plist_path.exists():
        return ServiceInstallResult(
            installed=False,
            plist_path=plist_path,
            message=f"No plist at {plist_path} — nothing to uninstall",
        )

    _launchctl_unload(plist_path)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    return ServiceInstallResult(
        installed=False,
        plist_path=plist_path,
        message=f"Unloaded and removed {plist_path}",
    )


def status() -> dict[str, object]:
    """Return a dict describing the launchd agent state + daemon state."""
    _require_macos()
    from chiefwiggum.daemon import is_daemon_running

    plist_path = plist_install_path()
    plist_installed = plist_path.exists()
    loaded = _launchctl_is_loaded(LABEL)

    daemon_running, daemon_pid = is_daemon_running()
    return {
        "label": LABEL,
        "plist_path": str(plist_path),
        "plist_installed": plist_installed,
        "launchd_loaded": loaded,
        "daemon_running": daemon_running,
        "daemon_pid": daemon_pid,
    }


def restart() -> ServiceInstallResult:
    """Ask launchd to kickstart the daemon (uses `launchctl kickstart -k`)."""
    _require_macos()
    plist_path = plist_install_path()
    if not plist_path.exists():
        return ServiceInstallResult(
            installed=False,
            plist_path=plist_path,
            message=f"No plist at {plist_path}; run `wig service install` first",
        )

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ServiceInstallResult(
            installed=True,
            plist_path=plist_path,
            message=f"kickstart failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    return ServiceInstallResult(
        installed=True,
        plist_path=plist_path,
        message=f"Kickstarted {LABEL}",
    )


# ----------------------------------------------------------------------------
# launchctl helpers (private)
# ----------------------------------------------------------------------------


def _launchctl_reload(plist_path: Path) -> None:
    uid = os.getuid()
    # Unload any existing version — best-effort.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
    )
    # Legacy fallback for older macOS.
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )
    # Load the new plist. Prefer bootstrap on modern macOS.
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        # Fallback to `load` for older macOS / for users with different setups.
        subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
            text=True,
        )


def _launchctl_unload(plist_path: Path) -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )


def _launchctl_is_loaded(label: str) -> bool:
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[-1] == label:
            return True
    return False
