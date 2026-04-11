"""Token usage formatting utilities for the CLI."""


def calculate_context_percentage(current_tokens: int, context_window: int) -> float:
    """Calculate the percentage of context window used.

    Args:
        current_tokens: Current number of tokens in context
        context_window: Maximum context window size

    Returns:
        Percentage of context window used (0-100)
    """
    if context_window <= 0:
        return 0.0
    return (current_tokens / context_window) * 100


def format_tokens(tokens: int) -> str:
    """Format token count for display.

    Args:
        tokens: Number of tokens

    Returns:
        Formatted string (e.g., "123K", "1.2M")
    """
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    elif tokens >= 1_000:
        return f"{tokens / 1_000:.0f}K"
    else:
        return str(tokens)
