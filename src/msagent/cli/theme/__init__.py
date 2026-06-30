from msagent.cli.theme import tokyo_day, tokyo_night  # noqa: F401
from msagent.cli.theme.console import ThemedConsole
from msagent.cli.theme.registry import get_theme
from msagent.core.settings import settings

# Use user setting if set, otherwise auto-detect
if settings.cli.theme is not None:
    _theme_name = settings.cli.theme
else:
    _theme_name = "tokyo-night"

theme = get_theme(_theme_name)
console = ThemedConsole(theme)
