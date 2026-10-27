import httpx


class LlmError(RuntimeError):
    """Raised on any LLM provider failure.

    Wraps the underlying SDK or subprocess exception via ``__cause__``
    so the original detail is preserved while callers can branch on a
    single, provider-agnostic exception type. The error message string
    is the only stable contract — providers may produce wildly
    different error shapes underneath.

    Parameters
    ----------
    message : str
        The error message.
    request_id : str or None, optional
        The provider's request id, when the failure was an answered
        request (an ``APIStatusError``). Shown to the user so a failure can
        be quoted to the provider's support.
    code : str or None, optional
        A machine-readable reason the caller can branch on (for example
        ``"effort_unsupported"``). ``None`` for an ordinary failure.

    Attributes
    ----------
    request_id : str or None
        As the parameter.
    code : str or None
        As the parameter.
    """

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code


class LlmAuthError(LlmError):
    """The provider rejected the credentials (an invalid or expired key or token).

    Distinct from a one-off failure: every further call would fail the
    same way, so a batch (the analyze phase) stops at the first one
    instead of failing every commit.
    """


class LlmKeyMissing(LlmError):
    """No API key is configured for the provider, so no call was made.

    Raised by the chat adapters before they build an SDK client. The chat
    route answers it with an ``error`` frame whose ``code`` is
    ``no_llm_key``, which the playground words for the portal's mode.
    """


UNREACHABLE_CAUSES = frozenset({"APIConnectionError", "APITimeoutError"})
"""The SDK exceptions (``anthropic`` and ``openai`` name them alike) that mean
the provider could not be reached, rather than that it answered with an error."""


def is_unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` (or anything in its ``__cause__`` chain) means "unreachable".

    True for the SDKs' connection and timeout errors
    (:data:`UNREACHABLE_CAUSES`) and for any :class:`httpx.TransportError`
    - a read timeout during a silent think, a connection dropped
    mid-stream - which the SDKs do not wrap once a stream has started.

    Parameters
    ----------
    exc : BaseException
        The exception to inspect.

    Returns
    -------
    bool
        ``True`` when the provider could not be reached or the stream was
        cut, ``False`` for an answered error.
    """
    cause: BaseException | None = exc
    while cause is not None:
        if type(cause).__name__ in UNREACHABLE_CAUSES or isinstance(
            cause, httpx.TransportError
        ):
            return True
        cause = cause.__cause__
    return False
