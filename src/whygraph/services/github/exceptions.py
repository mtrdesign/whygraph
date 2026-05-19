class GitHubError(RuntimeError):
    """Raised when a GitHub API call fails or returns a malformed payload.

    Wraps the underlying :class:`whygraph.core.ShellError` (when the
    failure originates in the ``gh`` subprocess) or a JSON-decode error
    with semantic context about which GitHub operation failed. The
    original exception is preserved via ``__cause__``.
    """
