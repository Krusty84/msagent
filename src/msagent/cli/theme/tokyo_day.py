"""Tokyo Day theme colors and palette for light terminal backgrounds."""

from dataclasses import dataclass

from rich.style import Style
from rich.theme import Theme

from msagent.cli.theme.base import BaseTheme
from msagent.cli.theme.detect import is_likely_xshell
from msagent.cli.theme.registry import register_theme


@dataclass
class TokyoDayColors:
    """Tokyo Day color scheme definition (light mode variant)."""

    # Light backgrounds
    light_gray: str = "#d5d6db"
    off_white: str = "#e1e2e7"
    pale_blue: str = "#e9e9ed"
    mist: str = "#f0f0f4"

    # Dark text colors
    deep_blue: str = "#343b58"
    slate_blue: str = "#565f89"
    gray_blue: str = "#6b7089"
    light_slate: str = "#8990a3"

    # Accent colors (adjusted for light background visibility)
    deep_azure: str = "#2e5a98"
    ocean_blue: str = "#166775"
    royal_purple: str = "#7847bd"
    forest_teal: str = "#166775"
    amber: str = "#8a5d00"
    magenta: str = "#9e3366"
    rust_orange: str = "#b55a1a"
    sky_blue: str = "#0f6d8f"
    steel_blue: str = "#8ca6bf"


@register_theme("tokyo-day")
class TokyoDayTheme(BaseTheme):
    """Tokyo Day theme implementation (light mode)."""

    def __init__(self):
        self.colors = TokyoDayColors()
        if is_likely_xshell():
            self._apply_xshell_palette()
        self.rich_theme = self._create_rich_theme()

    def _apply_xshell_palette(self) -> None:
        """Use higher-contrast ANSI-friendly colors for Xshell selection/rendering."""
        self.colors.light_gray = "#ffffff"
        self.colors.off_white = "#ffffff"
        self.colors.pale_blue = "#f5f5f5"
        self.colors.mist = "#fafafa"
        self.colors.deep_blue = "#000000"
        self.colors.slate_blue = "#303030"
        self.colors.gray_blue = "#4e4e4e"
        self.colors.light_slate = "#808080"
        self.colors.deep_azure = "#005faf"
        self.colors.ocean_blue = "#008787"
        self.colors.royal_purple = "#875fd7"
        self.colors.forest_teal = "#00875f"
        self.colors.amber = "#af8700"
        self.colors.magenta = "#af005f"
        self.colors.rust_orange = "#d75f00"
        self.colors.sky_blue = "#0087af"
        self.colors.steel_blue = "#808080"

    def _create_rich_theme(self) -> Theme:
        """Create Rich Theme with Tokyo Day colors."""
        c = self.colors
        return Theme(
            {
                # Basic text styles
                "default": Style(color=c.deep_blue),
                "primary": Style(color=c.deep_blue),
                "secondary": Style(color=c.slate_blue),
                "muted": Style(color=c.gray_blue),
                "muted.bold": Style(color=c.gray_blue, bold=True),
                "disabled": Style(color=c.light_slate),
                # Accent styles
                "accent": Style(color=c.deep_azure, bold=True),
                "accent.primary": Style(color=c.deep_azure),
                "accent.secondary": Style(color=c.ocean_blue),
                "accent.tertiary": Style(color=c.royal_purple),
                # Semantic styles
                "success": Style(color=c.forest_teal),
                "warning": Style(color=c.amber),
                "error": Style(color=c.magenta),
                "info": Style(color=c.deep_azure),
                # UI element styles
                "border": Style(color=c.light_slate),
                "prompt": Style(color=c.deep_azure, bold=True),
                "command": Style(color=c.royal_purple),
                "option": Style(color=c.ocean_blue),
                "indicator": Style(color=c.forest_teal),
                # Code syntax highlighting
                "code": Style(color=c.forest_teal, bold=False),
                "code.keyword": Style(color=c.royal_purple, bold=True),
                "code.string": Style(color=c.forest_teal),
                "code.number": Style(color=c.rust_orange),
                "code.comment": Style(color=c.gray_blue, italic=True),
                "code.operator": Style(color=c.sky_blue),
                # Markdown specific styles
                "markdown.code": Style(color=c.royal_purple, bold=True),
                # Special elements
                "timestamp": Style(color=c.gray_blue, italic=True),
            }
        )

    # Semantic color accessors for BaseTheme protocol
    @property
    def primary_text(self) -> str:
        return self.colors.deep_blue

    @property
    def secondary_text(self) -> str:
        return self.colors.slate_blue

    @property
    def muted_text(self) -> str:
        return self.colors.gray_blue

    @property
    def background(self) -> str:
        return self.colors.off_white

    @property
    def background_light(self) -> str:
        return self.colors.pale_blue

    @property
    def success_color(self) -> str:
        return self.colors.forest_teal

    @property
    def error_color(self) -> str:
        return self.colors.magenta

    @property
    def warning_color(self) -> str:
        return self.colors.amber

    @property
    def info_color(self) -> str:
        return self.colors.deep_azure

    @property
    def prompt_color(self) -> str:
        return self.colors.deep_azure

    @property
    def accent_color(self) -> str:
        return self.colors.ocean_blue

    @property
    def indicator_color(self) -> str:
        return self.colors.forest_teal

    @property
    def command_color(self) -> str:
        return self.colors.royal_purple

    @property
    def addition_color(self) -> str:
        return self.colors.forest_teal

    @property
    def deletion_color(self) -> str:
        return self.colors.magenta

    @property
    def context_color(self) -> str:
        return self.colors.slate_blue

    @property
    def approval_semi_active(self) -> str:
        return self.colors.deep_azure

    @property
    def approval_active(self) -> str:
        return self.colors.amber

    @property
    def approval_aggressive(self) -> str:
        return self.colors.royal_purple

    @property
    def selection_color(self) -> str:
        return self.colors.forest_teal

    @property
    def spinner_color(self) -> str:
        return self.colors.forest_teal

    @property
    def danger_color(self) -> str:
        return self.colors.magenta
