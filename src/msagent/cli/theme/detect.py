"""Terminal theme detection utilities.

Detects whether the terminal is using a dark or light color scheme
by querying the terminal's background color.
"""

import os
import re
import select
import sys

from prompt_toolkit.output.color_depth import ColorDepth


def is_likely_xshell() -> bool:
    """Best-effort detection for Xshell sessions."""
    hints = ("TERM_PROGRAM", "XTERM_PROGRAM", "LC_TERMINAL", "TERM", "COLORTERM")
    return any("xshell" in os.environ.get(name, "").lower() for name in hints)


def detect_rich_color_system() -> str | None:
    """Detect a safe Rich color system for the current terminal."""
    if is_likely_xshell():
        return "256"

    color_term = os.environ.get("COLORTERM", "").strip().lower()
    if color_term in {"truecolor", "24bit"}:
        return "truecolor"

    term = os.environ.get("TERM", "").strip().lower()
    if "256color" in term:
        return "256"
    if term:
        return "standard"
    return "truecolor"


def detect_prompt_toolkit_color_depth() -> ColorDepth:
    """Detect a safe prompt-toolkit color depth for the current terminal."""
    if is_likely_xshell():
        return ColorDepth.DEPTH_8_BIT

    color_term = os.environ.get("COLORTERM", "").strip().lower()
    if color_term in {"truecolor", "24bit"}:
        return ColorDepth.TRUE_COLOR

    term = os.environ.get("TERM", "").strip().lower()
    if "256color" in term:
        return ColorDepth.DEPTH_8_BIT
    if term:
        return ColorDepth.DEPTH_4_BIT
    return ColorDepth.TRUE_COLOR


def _detect_via_osc11() -> str | None:
    """Detect terminal theme using OSC 11 query.

    Sends an escape sequence to query the terminal's background color.
    Works with xterm-compatible terminals like iTerm2, Terminal.app,
    Kitty, Alacritty, GNOME Terminal, VS Code, etc.

    Returns:
        'dark', 'light', or None if detection fails.
    """
    # Only works with real TTYs
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    # Import here to avoid issues on non-Unix systems
    try:
        import termios
        import tty
    except ImportError:
        return None

    fd = sys.stdin.fileno()

    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return None

    try:
        tty.setraw(fd)

        # OSC 11 query: request background color
        # \033]11;?\033\\ is the xterm control sequence
        sys.stdout.write("\033]11;?\033\\")
        sys.stdout.flush()

        # Wait for response with 100ms timeout
        if select.select([sys.stdin], [], [], 0.1)[0]:
            response = ""
            # Read response character by character
            while select.select([sys.stdin], [], [], 0.05)[0]:
                char = sys.stdin.read(1)
                response += char
                # Stop if we've received the terminator
                if response.endswith("\\") or response.endswith("\a"):
                    break

            # Parse response: rgb:RRRR/GGGG/BBBB
            match = re.search(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", response)
            if match:
                # Take first 2 hex digits (scale to 0-255)
                r = int(match.group(1)[:2], 16)
                g = int(match.group(2)[:2], 16)
                b = int(match.group(3)[:2], 16)

                # Calculate perceived luminance using YCbCr formula
                luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
                return "light" if luminance > 0.5 else "dark"
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

    return None


def _detect_via_colorfgbg() -> str | None:
    """Detect terminal theme from COLORFGBG environment variable.

    This variable is set by some terminals (rxvt, etc.) in the format
    'foreground;background' using ANSI color indices.

    Returns:
        'dark', 'light', or None if detection fails.
    """
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        parts = colorfgbg.split(";")
        if len(parts) >= 2 and parts[-1].isdigit():
            bg = int(parts[-1])
            # 7 = white, 15 = bright white -> light theme
            return "light" if bg in (7, 15) else "dark"
    return None


def _detect_via_os() -> str | None:
    """Detect OS-level dark/light mode setting.

    Supports:
    - macOS: Uses 'defaults read' to check AppleInterfaceStyle
    - Linux: Uses 'gsettings' to check GTK theme (GNOME/GTK-based)

    Returns:
        'dark', 'light', or None if detection fails.
    """
    import subprocess

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            # Command succeeds if dark mode is enabled (returns "Dark")
            # Command fails (exit code 1) if light mode is active
            return "dark" if result.returncode == 0 else "light"
        except Exception:
            pass

    elif sys.platform == "linux":
        # Try GNOME/GTK first (gsettings)
        try:
            # Try freedesktop color-scheme first (modern)
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            stdout = result.stdout.strip().lower()

            # Fallback to gtk-theme if color-scheme not set
            if not stdout or stdout == "''":
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                stdout = result.stdout.strip().lower()

            if stdout and stdout != "''":
                if "-dark" in stdout or "'dark'" in stdout:
                    return "dark"
                return "light"
        except Exception:
            pass

        # Try KDE Plasma (kreadconfig5/kreadconfig6)
        for cmd in ["kreadconfig6", "kreadconfig5"]:
            try:
                result = subprocess.run(
                    [
                        cmd,
                        "--file",
                        "kdeglobals",
                        "--group",
                        "General",
                        "--key",
                        "ColorScheme",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if result.returncode == 0:
                    stdout = result.stdout.strip().lower()
                    if stdout:
                        return "dark" if "dark" in stdout else "light"
            except Exception:
                pass

    return None


def detect_terminal_theme() -> str:
    """Detect terminal theme with fallback chain.

    Tries multiple detection methods in order:
    1. OSC 11 terminal query (most accurate)
    2. COLORFGBG environment variable
    3. OS-level dark mode (macOS/Linux)
    4. Default to 'dark'

    Returns:
        'dark' or 'light'
    """
    # Xshell is a remote terminal emulator: its visible background is often
    # unrelated to the remote Linux desktop theme, so avoid OS-level fallback
    # there and trust terminal-originated hints only.
    if is_likely_xshell():
        result = _detect_via_osc11() or _detect_via_colorfgbg()
        return result or "dark"

    result = _detect_via_osc11() or _detect_via_colorfgbg() or _detect_via_os()
    return result or "dark"
