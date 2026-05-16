"""Rich styles and formatting utilities with theme support."""

import sys

from rich.console import Console

from msagent.cli.theme.base import BaseTheme


class ThemedConsole:
    """Console wrapper with configurable theme."""

    def __init__(self, console_theme: BaseTheme):
        self.console = Console(
            theme=console_theme.rich_theme,
            force_terminal=True,
            color_system="truecolor",
        )

    def print(self, *args, style: str = "default", **kwargs):
        """Print with theme-aware styling."""
        try:
            self.console.print(*args, style=style, **kwargs)
        except UnicodeEncodeError:
            with self.console.capture() as capture:
                self.console.print(*args, style=style, **kwargs)
            fallback_text = capture.get()
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(fallback_text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end="")

    def _print_status(self, icon_markup: str, fallback_prefix: str, content: str) -> None:
        """Print a status line with a plain-text fallback for narrow console encodings."""
        try:
            self.console.print(f"{icon_markup} {content}")
        except UnicodeEncodeError:
            print(f"{fallback_prefix} {content}")

    def print_error(self, content: str):
        """Print error message."""
        self._print_status("[error]\u274c[/error]", "[ERR]", content)

    def print_warning(self, content: str):
        """Print warning message."""
        self._print_status("[warning]\u26a0\ufe0f[/warning]", "[WARN]", content)

    def print_success(self, content: str):
        """Print success message."""
        self._print_status("[success]\u2705[/success]", "[OK]", content)

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
