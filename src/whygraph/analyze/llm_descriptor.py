"""LLM-driven descriptions of git diffs.

This is the raw service consumed by future orchestration (a crawler
that walks commits, parallelizes via :class:`ThreadPoolExecutor`, and
persists results to :attr:`Commit.llm_description`). The descriptor
itself owns *only* the prompt + LLM round-trip:

* No git access — the diff is an input.
* No DB writes — the :class:`Description` is an output.
* No concurrency — one diff in, one description out.

Composing those concerns lives in the (deferred) crawler module so this
class stays trivially testable: feed a stub :class:`LlmClient` and a
diff string, assert on the returned :class:`Description`.
"""

from __future__ import annotations

from whygraph.core.config import AnalyzeConfig
from whygraph.services.llm import (
    CompletionRequest,
    LlmClient,
    LlmClientFactory,
    LlmError,
)

from .description import Description
from .exceptions import AnalyzeError
from .prompt import render

_TRUNCATION_MARKER = "\n[truncated: {omitted} chars omitted]"


class LlmDescriptor:
    """Turn a git diff into a model-written description.

    The descriptor:

    1. Validates the diff is non-empty.
    2. Truncates to :attr:`max_diff_chars` if needed, appending an
       explicit marker so the model knows the input was clipped.
    3. Renders the prompt template with the (possibly truncated) diff.
    4. Calls :meth:`LlmClient.complete` with a single user message.
    5. Returns a :class:`Description` value object.

    Parameters
    ----------
    client : LlmClient
        Pre-configured adapter. Inject a stub in tests; in production,
        :meth:`from_config` builds one via :class:`LlmClientFactory`.
    max_diff_chars : int
        Cap on the diff body before prompting. Defaults to ``50_000``.
    timeout_sec : int or None
        Per-call timeout forwarded into the :class:`CompletionRequest`.
        ``None`` (default) defers to the adapter's bound default.
    prompt_template : str, optional
        Override the default prompt template. Must contain ``{diff}``.
        Mostly used in tests; production wiring relies on the default.

    Examples
    --------
    >>> descriptor = LlmDescriptor.from_config(get_config().analyze)
    >>> repo = Repository(Path.cwd())
    >>> commit = next(iter(repo.commits))
    >>> desc = descriptor.describe(repo.diff(commit))
    >>> print(desc.text)
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        max_diff_chars: int = 50_000,
        timeout_sec: int | None = None,
        prompt_template: str | None = None,
    ) -> None:
        if max_diff_chars < 1:
            raise ValueError(f"max_diff_chars must be >= 1, got {max_diff_chars}")
        self._client = client
        self._max_diff_chars = max_diff_chars
        self._timeout_sec = timeout_sec
        self._prompt_template = prompt_template

    def __repr__(self) -> str:
        return (
            f"LlmDescriptor(client={self._client!r}, "
            f"max_diff_chars={self._max_diff_chars})"
        )

    @classmethod
    def from_config(
        cls,
        config: AnalyzeConfig,
        *,
        factory: LlmClientFactory | None = None,
    ) -> "LlmDescriptor":
        """Build a descriptor from an :class:`AnalyzeConfig`.

        Parameters
        ----------
        config : AnalyzeConfig
            Typically ``get_config().analyze``.
        factory : LlmClientFactory, optional
            Override the factory used to resolve ``config.provider``
            into an :class:`LlmClient`. Defaults to a fresh
            :class:`LlmClientFactory` bound to the process-wide
            :class:`LlmConfig`. Inject a custom factory in tests to
            bind a stub adapter without touching global state.

        Returns
        -------
        LlmDescriptor
            A descriptor ready to call :meth:`describe`.

        Raises
        ------
        whygraph.services.llm.LlmError
            If ``config.provider`` is not registered with the factory.
            Propagated directly so the user sees the available providers.
        """
        factory = factory if factory is not None else LlmClientFactory()
        client = factory.make(config.provider)
        return cls(
            client,
            max_diff_chars=config.max_diff_chars,
            timeout_sec=config.timeout_sec,
        )

    def describe(self, diff: str) -> Description:
        """Generate a description for one diff.

        Parameters
        ----------
        diff : str
            Raw textual diff (e.g. from
            :meth:`whygraph.services.git.Repository.diff`).

        Returns
        -------
        Description
            The model's description plus provenance.

        Raises
        ------
        AnalyzeError
            If ``diff`` is empty/whitespace-only, or if the underlying
            :meth:`LlmClient.complete` raises :class:`LlmError`. The
            original exception is preserved as ``__cause__``.
        """
        if not diff or not diff.strip():
            raise AnalyzeError("empty diff: nothing to describe")

        body, truncated = self._truncate(diff)
        prompt = render(body, template=self._prompt_template)
        request = CompletionRequest.of(prompt, timeout_sec=self._timeout_sec)

        try:
            response = self._client.complete(request)
        except LlmError as exc:
            raise AnalyzeError(f"LLM call failed: {exc}") from exc

        return Description(
            text=response.text.strip(),
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            truncated=truncated,
        )

    def _truncate(self, diff: str) -> tuple[str, bool]:
        """Clip ``diff`` to :attr:`max_diff_chars` and append a marker.

        Returns the (possibly clipped) body plus a flag indicating
        whether truncation happened. The marker wording is stable so
        previously-generated descriptions remain recognisable.
        """
        if len(diff) <= self._max_diff_chars:
            return diff, False
        omitted = len(diff) - self._max_diff_chars
        return diff[: self._max_diff_chars] + _TRUNCATION_MARKER.format(
            omitted=omitted
        ), True
