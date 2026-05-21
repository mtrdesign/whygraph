"""Factory class for constructing :class:`LlmClient` adapters by name.

Construction-time inputs:

* a :class:`whygraph.core.config.LlmConfig` (defaults to
  ``get_config().llm`` — typically what production wiring wants).
* a registry of ``provider tag → (adapter class, bound config)``.

Each factory instance owns its own registry copy, so calling
:meth:`register` does not affect other factories. The five built-in
adapters (anthropic, openai, deepseek, ollama, claude-cli) are
pre-resolved against the bound :class:`LlmConfig` at construction time;
third-party adapters added via :meth:`register` bring their own typed
config instance instead.
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar

from whygraph.core import get_config
from whygraph.core.config import LlmConfig

from .anthropic import AnthropicAdapter
from .claude_cli import ClaudeCliAdapter
from .client import LlmClient
from .deepseek import DeepSeekAdapter
from .exceptions import LlmError
from .ollama import OllamaAdapter
from .openai import OpenAIAdapter

# provider tag → (adapter class, attribute name on LlmConfig).
# Used only by :meth:`LlmClientFactory.__init__` to resolve the bound
# config into (adapter, config_obj) entries.
_BUILTIN_DEFAULTS: dict[str, tuple[type[LlmClient], str]] = {
    AnthropicAdapter.provider: (AnthropicAdapter, "anthropic"),
    OpenAIAdapter.provider: (OpenAIAdapter, "openai"),
    DeepSeekAdapter.provider: (DeepSeekAdapter, "deepseek"),
    OllamaAdapter.provider: (OllamaAdapter, "ollama"),
    ClaudeCliAdapter.provider: (ClaudeCliAdapter, "claude_cli"),
}


class LlmClientFactory:
    """Construct :class:`LlmClient` instances by provider tag.

    Parameters
    ----------
    config : LlmConfig, optional
        The LLM configuration to read from. ``None`` (default) pulls
        the process-wide config via :func:`whygraph.core.get_config`.

    Attributes
    ----------
    providers : tuple[str, ...]
        Sorted tuple of registered provider tags. Read-only view of
        the per-instance registry.

    Examples
    --------
    >>> factory = LlmClientFactory()                # doctest: +SKIP
    >>> client = factory.make("anthropic")          # doctest: +SKIP
    >>> for_test = LlmClientFactory(config=custom_llm_config)  # doctest: +SKIP
    >>> factory.register("groq", GroqAdapter, config=groq_cfg) # doctest: +SKIP
    """

    BUILTIN_PROVIDERS: ClassVar[tuple[str, ...]] = tuple(_BUILTIN_DEFAULTS)
    """Provider tags of the adapters built into this package."""

    def __init__(self, config: LlmConfig | None = None) -> None:
        self._config = config if config is not None else get_config().llm
        # Pre-resolve each built-in entry into (adapter_cls, config_obj).
        # Registered providers later store the config_obj directly.
        self._registry: dict[str, tuple[type[LlmClient], object]] = {
            tag: (cls, getattr(self._config, attr))
            for tag, (cls, attr) in _BUILTIN_DEFAULTS.items()
        }

    @property
    def providers(self) -> tuple[str, ...]:
        """Sorted tuple of currently registered provider tags."""
        return tuple(sorted(self._registry))

    def make(
        self, provider: str, *, model: str | None = None, **overrides
    ) -> LlmClient:
        """Construct an :class:`LlmClient` for the given provider tag.

        Parameters
        ----------
        provider : str
            One of :attr:`providers`. The built-ins are listed in
            :attr:`BUILTIN_PROVIDERS`; third-party tags added via
            :meth:`register` are also accepted.
        model : str, optional
            Override the model bound by the provider's config. ``None``
            (default) uses the model from the registered config section.
            Every provider config is a dataclass with a ``model`` field,
            so the override is applied via :func:`dataclasses.replace`.
        **overrides
            Forwarded to the adapter's ``from_config`` (e.g. ``client=``
            to inject a stub SDK in tests).

        Returns
        -------
        LlmClient
            A configured adapter ready to call :meth:`LlmClient.complete`.

        Raises
        ------
        LlmError
            If ``provider`` is not in :attr:`providers`.
        """
        entry = self._registry.get(provider)
        if entry is None:
            raise LlmError(
                f"unknown LLM provider: {provider!r}; "
                f"available: {self.providers}"
            )
        cls, config_obj = entry
        if model is not None:
            config_obj = dataclasses.replace(config_obj, model=model)
        return cls.from_config(config_obj, **overrides)

    def register(
        self,
        provider: str,
        adapter_cls: type[LlmClient],
        *,
        config: object,
    ) -> None:
        """Add a provider to this factory's registry.

        Parameters
        ----------
        provider : str
            Tag used as the first argument to :meth:`make`. Must match
            ``adapter_cls.provider`` for consistency, though the
            factory does not enforce it.
        adapter_cls : type[LlmClient]
            The adapter class. Must define a ``from_config(config)``
            classmethod compatible with the supplied ``config`` object.
        config : object
            Typed config instance passed verbatim to
            ``adapter_cls.from_config``. Lives outside
            :class:`LlmConfig`; the factory does not introspect it.

        Notes
        -----
        Re-registering an existing tag overwrites the previous entry —
        useful for swapping a built-in adapter for a fork without
        editing the package. Other :class:`LlmClientFactory` instances
        are unaffected (registries are per-instance).
        """
        self._registry[provider] = (adapter_cls, config)
