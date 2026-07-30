"""Small cross-cutting helpers with no home of their own."""

from typing import Any, Dict

LIKE_ESCAPE_CHAR = "\\"
"""The character :func:`like_escape` escapes with — pass it as SQL ``ESCAPE``."""


def like_escape(value: str) -> str:
    """Escape SQL ``LIKE`` metacharacters in ``value``.

    ``LIKE`` treats ``%`` as "any run of characters" and ``_`` as "any single
    character", so an unescaped path prefix silently widens the match. ``_``
    is the one that bites in practice: this repo is full of paths like
    ``commit_file_change.py``, and ``LIKE 'src/whygraph/chat_%'`` would match
    a sibling ``chatX/`` directory.

    Parameters
    ----------
    value : str
        Raw text destined for the right-hand side of a ``LIKE``.

    Returns
    -------
    str
        ``value`` with ``\\``, ``%``, and ``_`` each prefixed by
        :data:`LIKE_ESCAPE_CHAR`. The query **must** declare
        ``ESCAPE '\\'`` for this to take effect.

    Examples
    --------
    >>> like_escape("src/pkg_a")
    'src/pkg\\\\_a'
    """
    for char in (LIKE_ESCAPE_CHAR, "%", "_"):
        value = value.replace(char, LIKE_ESCAPE_CHAR + char)
    return value


class SingletonMeta(type):
    """
    The SingleTonMeta util for defining singleton classes.
    """

    _instances: Dict[Any, Any] = {}

    def __call__(cls, *args, **kwargs):
        """
        Possible changes to the value of the `__init__` argument do not affect
        the returned instance.
        """
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance

        return cls._instances[cls]
