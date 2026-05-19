class GitError(RuntimeError):
    """Raised when a ``git`` subprocess fails or returns malformed output.

    Wraps the underlying :class:`whygraph.core.ShellError` (available via
    ``__cause__``) with semantic context about which git operation failed.
    """
