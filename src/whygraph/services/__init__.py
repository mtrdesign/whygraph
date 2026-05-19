"""External-system adapters.

Each module in this package wraps one external system (git, GitHub, ...)
behind a small typed client class plus value objects. Modules import from
:mod:`whygraph.core` but never from each other's higher-level callers.

See ``services/git/`` for the canonical shape: ``<System>Error``
exception, frozen value-object dataclasses, and a ``<System>Client``-style
class that holds a :class:`whygraph.core.Shell` for all subprocess work.
"""
