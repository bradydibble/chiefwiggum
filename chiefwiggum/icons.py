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

# Error category icons
ICON_ERROR_PERMISSION = "\u26d4"  # ⛔ No entry (permission denied)
ICON_ERROR_API = "\u26a1"         # ⚡ Lightning (API/rate limit)
ICON_ERROR_TOOL = "\u2699"        # ⚙ Gear (tool failure)
ICON_ERROR_GENERAL = "\u26a0"     # ⚠ Warning (general error)

# Priority indicators (shape + color for accessibility)
ICON_HIGH = "■"             # Filled square (red)
ICON_MEDIUM = "◆"           # Diamond (yellow)
ICON_LOWER = "○"            # Empty circle (blue)
ICON_POLISH = "·"           # Dot (dim)

# Priority symbols (alternate set)
PRIORITY_SYMBOLS = {
    "HIGH": "■",      # Filled square
    "MEDIUM": "◆",    # Diamond
    "LOWER": "○",     # Empty circle
    "POLISH": "·",    # Dot
}

# Selection
ICON_SELECTED = "\u25b8"    # ▸ Current selection

# Activity/spinner
SPINNER = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]
SPINNER_BARS = ["\u2581", "\u2582", "\u2583", "\u2584", "\u2585", "\u2586", "\u2587", "\u2588"]

# Progress bar characters
PROGRESS_FILLED = "█"       # Filled block
PROGRESS_EMPTY = "░"        # Empty block
PROGRESS_PARTIAL = "▓"      # Partial fill

# Capacity bar characters
CAPACITY_FILLED = "▓"       # Filled capacity
CAPACITY_EMPTY = "░"        # Empty capacity

# Stall indicator
ICON_STALL = "⏸"            # Pause symbol for stalled tasks

# Status symbols (shape + color for accessibility)
STATUS_SYMBOLS = {
    "pending": "○",         # Empty circle
    "in_progress": "●",     # Filled circle
    "completed": "✓",       # Checkmark
    "failed": "✗",          # X mark
    "retry_pending": "↻",   # Rotation arrow
}

# Alert type icons
ICON_ALERT_CRITICAL = "✗"   # Critical alert (failures)
ICON_ALERT_WARNING = "⚠"    # Warning alert
ICON_ALERT_INFO = "ℹ"       # Info alert

# Separators
SEP_VERTICAL = "\u2502"     # │ Vertical bar

# Daemon/instance
ICON_DAEMON = "\u25c9"      # ◉ Daemon indicator

# ============================================================================
# Semantic Colors
# ============================================================================
COLOR_SUCCESS = "bright_green"
COLOR_WARNING = "orange1"
COLOR_ERROR = "red3"
COLOR_INFO = "dodger_blue2"
COLOR_ACCENT = "bright_cyan"
COLOR_MUTED = "grey58"

# Background colors
BG_SELECTED = "grey27"
BG_STRIPE = "grey15"
BG_HEADER = "grey19"

# Panel border colors by context
BORDER_INSTANCES = "bright_green"
BORDER_TASKS = "gold1"
BORDER_OVERLAY = "bright_cyan"
BORDER_ERROR = "red3"
BORDER_SPAWN = "bright_green"
BORDER_STATS = "dodger_blue2"
BORDER_ALERTS = "red3"

# Alert colors
COLOR_ALERT_CRITICAL = "red3"
COLOR_ALERT_WARNING = "orange1"
COLOR_ALERT_INFO = "dodger_blue2"
COLOR_OVERDUE = "yellow3"

# ============================================================================
# Style Constants
# ============================================================================
STYLE_ACTIVE = "bold bright_green"
STYLE_IDLE = "yellow3"
STYLE_PAUSED = "blue"
STYLE_CRASHED = "bold red"
STYLE_STALE = "bold orange1"
STYLE_DIM = "dim"
STYLE_KEY_PRIMARY = "bold gold1"
STYLE_KEY_SECONDARY = "cyan"
STYLE_KEY_SHIFT = "bold magenta"
STYLE_KEY_CTRL = "bold red"
STYLE_HIGHLIGHT = "bold white on grey27"

# Table row styles
STYLE_TABLE_ROW_EVEN = "on grey15"
STYLE_TABLE_ROW_ODD = ""
