"""Feature flags."""

FLAGS = {"wide": False}


def is_on(name: str) -> bool:
    """Return whether a flag is on."""
    return FLAGS.get(name, False)
