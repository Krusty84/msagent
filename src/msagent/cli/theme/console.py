"""Rich styles and formatting utilities with theme support."""

import os

from rich.console import Console

from msagent.cli.theme.base import BaseTheme


def _detect_color_system() -> str:
    """Detect the terminal's color capability.

    Returns one of 'truecolor', '256', 'standard', or 'auto'.
    """
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return "truecolor"

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in ("iterm.app", "hyper"):
        return "truecolor"

    term = os.environ.get("TERM", "").lower()
    if "256color" in term:
        return "256"
    if term in ("xterm", "screen", "vt100", "linux", "ansi"):
        return "256"

    return "auto"


class ThemedConsole:
    """Console wrapper with configurable theme."""

    def __init__(self, console_theme: BaseTheme):
        self.console = Console(
            theme=console_theme.rich_theme,
            force_terminal=True,
            color_system=_detect_color_system(),
        )

    def print(self, *args, style: str = "default", **kwargs):
        """Print with theme-aware styling."""
        self.console.print(*args, style=style, **kwargs)

    def print_error(self, content: str):
        """Print error message."""
        self.console.print(f"[error]\u274c[/error] {content}")

    def print_warning(self, content: str):
        """Print warning message."""
        self.console.print(f"[warning]\u26a0\ufe0f[/warning] {content}")

    def print_success(self, content: str):
        """Print success message."""
        self.console.print(f"[success]\u2705[/success] {content}")

    def clear(self):
        """Clear the console."""
        self.console.clear()

    def capture(self):
        """Capture console output for measuring rendered lines."""
        return self.console.capture()

    @property
    def width(self):
        """Get console width."""
        return self.console.width
