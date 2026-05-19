class LlmError(RuntimeError):
    """Raised on any LLM provider failure.

    Wraps the underlying SDK or subprocess exception via ``__cause__``
    so the original detail is preserved while callers can branch on a
    single, provider-agnostic exception type. The error message string
    is the only stable contract — providers may produce wildly
    different error shapes underneath.
    """
