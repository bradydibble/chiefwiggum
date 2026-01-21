"""Unicode icons for TUI elements."""

# Status icons
ICON_ACTIVE = "\u25cf"      # ● Running
ICON_IDLE = "\u25d0"        # ◐ Waiting
ICON_PAUSED = "\u2389"      # ⎉ Paused
ICON_STOPPED = "\u25a0"     # ■ Stopped
ICON_CRASHED = "\u2717"     # ✗ Error
ICON_STALE = "\u26a0"       # ⚠ Stale warning

# Task status
ICON_PENDING = "\u25cb"     # ○ Queue
ICON_WORKING = "\u25b6"     # ▶ In progress
ICON_DONE = "\u2713"        # ✓ Completed
ICON_FAILED = "\u2717"      # ✗ Failed
ICON_RETRY = "\u21bb"       # ↻ Retry
ICON_RELEASED = "\u2192"    # → Released

# Priority indicators
ICON_HIGH = "\u2261"        # ≡ Three bars
ICON_MEDIUM = "\u2550"      # ═ Two bars
ICON_LOWER = "\u2500"       # ─ One bar
ICON_POLISH = "\u00b7"      # · Dot

# Selection
ICON_SELECTED = "\u25b8"    # ▸ Current selection

# Activity/spinner
SPINNER = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]

# Separators
SEP_VERTICAL = "\u2502"     # │ Vertical bar

# Daemon/instance
ICON_DAEMON = "\u25c9"      # ◉ Daemon indicator

# Style Constants
STYLE_ACTIVE = "bold green"
STYLE_IDLE = "yellow"
STYLE_PAUSED = "blue"
STYLE_CRASHED = "bold red"
STYLE_STALE = "bold yellow"
STYLE_DIM = "dim"
STYLE_KEY_PRIMARY = "bold yellow"
STYLE_KEY_SECONDARY = "cyan"
STYLE_KEY_SHIFT = "bold magenta"
STYLE_KEY_CTRL = "bold red"
STYLE_HIGHLIGHT = "bold white on grey23"
