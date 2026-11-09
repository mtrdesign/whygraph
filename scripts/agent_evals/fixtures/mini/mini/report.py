"""A tiny report formatter."""

TITLE = "Items"


def render(count: int) -> str:
    """Return one report line for a count of items."""
    return f"{TITLE}: {count}"
