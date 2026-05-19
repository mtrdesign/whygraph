"""Abstract port for LLM completion clients (ports-and-adapters).

Defines :class:`LlmClient`, the single interface every adapter
implements. Caller code depends only on this class — swapping
providers is a wiring change at the construction site, not a per-call
edit.
"""

from __future__ import annotations

import abc
from typing import Any

from .types import CompletionRequest, CompletionResponse


class LlmClient(abc.ABC):
    """Abstract port for an LLM completion client.

    Subclasses set the class-level :attr:`provider` identifier and
    implement :meth:`complete`. The base class only owns the bound
    ``model`` and a sensible :meth:`__repr__`.

    Attributes
    ----------
    provider : str
        Class-level provider tag, e.g. ``"anthropic"``. Used by
        :func:`whygraph.services.llm.make_client` for registry lookup
        and by :class:`CompletionResponse` to attribute the result.
    model : str
        Instance-bound model identifier.
    """

    provider: str

    def __init__(self, *, model: str) -> None:
        self.model = model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, model={self.model!r})"

    @classmethod
    @abc.abstractmethod
    def from_config(cls, config: Any, **overrides: Any) -> "LlmClient":
        """Build an adapter instance from its typed config section.

        Each adapter narrows ``config`` to its own concrete type (e.g.
        :class:`whygraph.core.config.AnthropicConfig`) in the override
        signature. The abstract method types ``config`` as ``Any`` so
        the port can accommodate every adapter's specific config
        dataclass without LSP violations — ``Any`` is treated as
        bidirectionally compatible by static type checkers.

        ``**overrides`` are forwarded to the underlying constructor
        (typically ``client=`` to inject a stub SDK in tests).

        Parameters
        ----------
        config : Any
            The provider's typed config section. Concrete adapters
            narrow this to their own dataclass.
        **overrides
            Non-config injectables passed straight through to the
            adapter's constructor.

        Returns
        -------
        LlmClient
            A configured adapter instance.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one completion synchronously.

        Parameters
        ----------
        request : CompletionRequest
            The messages and per-call overrides.

        Returns
        -------
        CompletionResponse
            Normalized result; provider-specific detail is preserved
            in :attr:`CompletionResponse.raw` where the provider gives
            it back.

        Raises
        ------
        LlmError
            On any provider failure (auth, network, timeout, malformed
            output, non-zero CLI exit, …). The originating exception
            is preserved as ``__cause__``.
        """
        raise NotImplementedError
