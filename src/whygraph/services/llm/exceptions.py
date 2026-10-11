class LlmError(RuntimeError):
    """Raised on any LLM provider failure.

    Wraps the underlying SDK or subprocess exception via ``__cause__``
    so the original detail is preserved while callers can branch on a
    single, provider-agnostic exception type. The error message string
    is the only stable contract — providers may produce wildly
    different error shapes underneath.
    """


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
