class GitHubError(RuntimeError):
    """Raised when a GitHub API call fails or returns a malformed payload.

    Wraps the underlying :class:`whygraph.core.ShellError` (when the
    failure originates in the ``gh`` subprocess) or a JSON-decode error
    with semantic context about which GitHub operation failed. The
    original exception is preserved via ``__cause__``.
    """


class RepoAccessError(GitHubError):
    """Raised by the repository access probe with a machine-readable ``code``.

    Attributes
    ----------
    code : str
        ``"bad_token"``, ``"no_access"`` or ``"not_found"``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
