"""Non-blocking keyboard input for TUI dashboard."""

import select
import sys
import termios
import threading
import tty
from queue import Empty, Queue
from typing import Optional


class KeyboardListener:
    """Threaded keyboard listener using tty raw mode."""

    def __init__(self):
        self._queue: Queue[str] = Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._old_settings = None

    def start(self) -> None:
        """Start the keyboard listener thread."""
        self._running = True
        self._old_settings = termios.tcgetattr(sys.stdin)
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the keyboard listener and restore terminal settings."""
        self._running = False
        if self._old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def get_key(self) -> Optional[str]:
        """Get the next key from the queue, or None if empty."""
        try:
            return self._queue.get_nowait()
        except Empty:
            return None

    def _listen(self) -> None:
        """Listen for keyboard input in a loop."""
        try:
            tty.setcbreak(sys.stdin.fileno())
            while self._running:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1)
                    if char == "\x1b":
                        char = self._read_escape_sequence()
                    self._queue.put(char)
        except Exception:
            pass

    def _read_escape_sequence(self) -> str:
        """Read and interpret an escape sequence."""
        if select.select([sys.stdin], [], [], 0.05)[0]:
            if sys.stdin.read(1) == "[":
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    code = sys.stdin.read(1)
                    return {
                        "A": "UP",
                        "B": "DOWN",
                        "C": "RIGHT",
                        "D": "LEFT",
                    }.get(code, "ESCAPE")
        return "ESCAPE"
