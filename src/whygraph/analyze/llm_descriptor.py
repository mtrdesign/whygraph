"""LLM-driven descriptions of git diffs.

This is the raw service consumed by
:class:`~whygraph.scan.analyze_crawler.AnalyzeCrawler`, which walks
commits, parallelizes via :class:`ThreadPoolExecutor`, and persists
results to ``commit.llm_description``. The descriptor itself owns *only*
the prompt + LLM round-trip(s):

* No git access — the diff is an input.
* No DB writes — the :class:`Description` is an output.
* No concurrency — one diff in, one description out. A diff over
  :attr:`max_diff_chars` is split into per-file chunks and described
  with one *sequential* LLM call per chunk plus a synthesis call; no
  threads are spawned, since the crawler already parallelizes commits.

Composing those concerns lives in the crawler module so this class
stays trivially testable: feed a stub :class:`LlmClient` and a diff
string, assert on the returned :class:`Description`.
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
from .diff_split import split_into_chunks
from .exceptions import AnalyzeError
from .prompt import SYNTHESIS_PLACEHOLDER, Prompt, render, resolve

_TRUNCATION_MARKER = "\n[truncated: {omitted} chars omitted]"

# Prompt component tag — the subtree under ``analyze/prompts/`` that
# holds this class's ``system`` / ``task`` markdown files.
_PROMPT_COMPONENT = "llm_descriptor"


def _sum_tokens(values: list[int | None]) -> int | None:
    """Sum reported token counts, ignoring the ones a provider omitted.

    Returns ``None`` only when *every* value is ``None`` — so a summed
    count is absent only when no call reported one, not merely when one
    call did.
    """
    present = [value for value in values if value is not None]
    return sum(present) if present else None


class LlmDescriptor:
    """Turn a git diff into a model-written description.

    The descriptor:

    1. Validates the diff is non-empty.
    2. A diff within :attr:`max_diff_chars` is described in a single LLM
       call: render the ``describe`` prompt, call
       :meth:`LlmClient.complete`, return a :class:`Description`.
    3. A diff over :attr:`max_diff_chars` is split at file boundaries
       into chunks (see
       :func:`whygraph.analyze.diff_split.split_into_chunks`); each
       chunk is described with its own call, then one synthesis call
       merges the chunk descriptions into a single :class:`Description`.
    4. A chunk that is itself over :attr:`max_diff_chars` (a single huge
       file) is truncated with an explicit marker so the model knows
       its input was clipped.
    5. For a split diff the returned :class:`Description` sums its token
       counts across every call, and ``truncated`` is set if any chunk
       was clipped.

    Parameters
    ----------
    client : LlmClient
        Pre-configured adapter. Inject a stub in tests; in production,
        :meth:`from_config` builds one via :class:`LlmClientFactory`.
    max_diff_chars : int
        Both the per-chunk truncation cap and the threshold above which
        a diff is split into per-file chunks. Defaults to ``50_000``.
    timeout_sec : int or None
        Per-call timeout forwarded into the :class:`CompletionRequest`.
        ``None`` (default) defers to the adapter's bound default.
    describe_prompt : Prompt, optional
        Override the ``describe`` prompt — the ``system`` + ``task``
        pair. When ``None`` (default), it is resolved from markdown by
        the client's provider and model — see
        :func:`whygraph.analyze.prompt.resolve`. An explicit
        :class:`~whygraph.analyze.prompt.Prompt` skips resolution; its
        ``task`` should contain the
        :data:`~whygraph.analyze.prompt.PLACEHOLDER` token. Mostly used
        in tests and one-off overrides.
    synthesis_prompt : Prompt, optional
        Override the ``synthesis`` prompt, used to merge per-chunk
        descriptions of a split diff. Resolution mirrors
        ``describe_prompt``; an explicit
        :class:`~whygraph.analyze.prompt.Prompt` ``task`` should contain
        the :data:`~whygraph.analyze.prompt.SYNTHESIS_PLACEHOLDER` token.

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
        describe_prompt: Prompt | None = None,
        synthesis_prompt: Prompt | None = None,
    ) -> None:
        if max_diff_chars < 1:
            raise ValueError(f"max_diff_chars must be >= 1, got {max_diff_chars}")
        self._client = client
        self._max_diff_chars = max_diff_chars
        self._timeout_sec = timeout_sec
        self._describe_prompt = (
            describe_prompt
            if describe_prompt is not None
            else resolve(_PROMPT_COMPONENT, "describe", client.provider, client.model)
        )
        self._synthesis_prompt = (
            synthesis_prompt
            if synthesis_prompt is not None
            else resolve(_PROMPT_COMPONENT, "synthesis", client.provider, client.model)
        )

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
        client = factory.make(config.provider, model=config.model)
        return cls(
            client,
            max_diff_chars=config.max_diff_chars,
            timeout_sec=config.timeout_sec,
        )

    def describe(self, diff: str) -> Description:
        """Generate a description for one diff.

        A diff within :attr:`max_diff_chars` is described in a single
        call. A larger diff is split into per-file chunks, each chunk is
        described separately, and a final synthesis call merges the
        results — see the class docstring.

        Parameters
        ----------
        diff : str
            Raw textual diff (e.g. from
            :meth:`whygraph.services.git.Repository.diff`).

        Returns
        -------
        Description
            The model's description plus provenance. For a split diff,
            token counts are summed across every call and ``truncated``
            is ``True`` if any chunk was clipped.

        Raises
        ------
        AnalyzeError
            If ``diff`` is empty/whitespace-only, or if any underlying
            :meth:`LlmClient.complete` raises :class:`LlmError`. The
            original exception is preserved as ``__cause__``.
        """
        if not diff or not diff.strip():
            raise AnalyzeError("empty diff: nothing to describe")

        if len(diff) <= self._max_diff_chars:
            return self._describe_one(diff)

        chunks = split_into_chunks(diff, self._max_diff_chars)
        if len(chunks) == 1:
            # One file larger than the cap — nothing to synthesise; the
            # lone chunk is truncated and described on its own.
            return self._describe_one(chunks[0])

        parts = [self._describe_one(chunk) for chunk in chunks]
        return self._synthesize(parts)

    def _describe_one(self, body: str) -> Description:
        """Describe one diff body — a whole diff or a single chunk.

        Truncates ``body`` to :attr:`max_diff_chars` if needed, renders
        the ``describe`` prompt, and runs one completion. This is the
        unit the chunk-split path calls once per chunk.
        """
        clipped, truncated = self._truncate(body)
        task = render(self._describe_prompt.task, clipped)
        request = CompletionRequest.of(
            task,
            system=self._describe_prompt.system,
            timeout_sec=self._timeout_sec,
        )

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

    def _synthesize(self, parts: list[Description]) -> Description:
        """Merge per-chunk descriptions into one via a final LLM call.

        The chunk descriptions are joined, labelled, and fed through the
        ``synthesis`` prompt. Token counts on the returned
        :class:`Description` are summed across every chunk call *and*
        this synthesis call; ``truncated`` is ``True`` if any chunk was
        clipped by :meth:`_truncate`.
        """
        joined = "\n\n".join(
            f"--- chunk {n} ---\n{part.text}" for n, part in enumerate(parts, start=1)
        )
        task = render(
            self._synthesis_prompt.task, joined, placeholder=SYNTHESIS_PLACEHOLDER
        )
        request = CompletionRequest.of(
            task,
            system=self._synthesis_prompt.system,
            timeout_sec=self._timeout_sec,
        )

        try:
            response = self._client.complete(request)
        except LlmError as exc:
            raise AnalyzeError(f"LLM synthesis call failed: {exc}") from exc

        return Description(
            text=response.text.strip(),
            model=response.model,
            provider=response.provider,
            input_tokens=_sum_tokens(
                [p.input_tokens for p in parts] + [response.input_tokens]
            ),
            output_tokens=_sum_tokens(
                [p.output_tokens for p in parts] + [response.output_tokens]
            ),
            truncated=any(p.truncated for p in parts),
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
