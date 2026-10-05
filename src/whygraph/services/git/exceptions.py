class GitError(RuntimeError):
    """Raised when a ``git`` subprocess fails or returns malformed output.

    Wraps the underlying :class:`whygraph.core.ShellError` (available via
    ``__cause__``) with semantic context about which git operation failed.
    """


class InvalidRepoUrlError(GitError):
    """Raised when a clone URL is not a plain ``<GitHub URL>/<owner>/<repo>``.

    Also raised when ``WHYGRAPH_GITHUB_URL`` itself is not a usable git host.

    A distinct subclass so a caller (the portal's add-project endpoint) can
    report a bad URL separately from a failed ``git`` run.
    """
