"""LLM-driven rationale for a piece of code's change history.

This is the raw service that turns an evidence bundle — scanned commits
with their linked pull requests and issues, optionally enriched with the
target symbol's code-graph context — into a structured
:class:`~whygraph.analyze.Rationale` card explaining *why* the code exists.
It owns only the prompt + LLM round-trip:

* No git or database access — the evidence is an input.
* No persistence — the :class:`~whygraph.analyze.Rationale` is an output.
* No chunking — one bundle in, one card out, in a single LLM call.

Gathering the evidence (git blame, database lookups, grouping pull requests
and issues onto commits) is intentionally out of scope so this class stays
trivially testable: feed a stub :class:`LlmClient` and a list of
:class:`~whygraph.analyze.CommitEvidence`, assert on the returned
:class:`~whygraph.analyze.Rationale`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from whygraph.core.config import RationaleConfig
from whygraph.db.models import Commit, Issue, PullRequest
from whygraph.services.codegraph import Relation, SymbolContext
from whygraph.services.llm import (
    CompletionRequest,
    LlmClient,
    LlmClientFactory,
    LlmError,
)

from .exceptions import AnalyzeError, RationaleError
from .prompt import RATIONALE_PLACEHOLDER, Prompt, render, resolve
from .rationale import CommitEvidence, Rationale

# Prompt component tag — the subtree under ``analyze/prompts/`` that holds
# this class's ``system`` / ``task`` markdown files.
_PROMPT_COMPONENT = "rationale_generator"

# The rationale JSON schema, split by the validation each key needs.
_REQUIRED_STR_FIELDS = ("purpose", "why")
_REQUIRED_LIST_FIELDS = ("constraints", "tradeoffs", "risks")


def _short_sha(sha: str) -> str:
    """Return the first eight characters of a commit SHA."""
    return sha[:8]


def _labels_suffix(raw: str) -> str:
    """Render a JSON-encoded label list as a `` [a, b]`` suffix.

    ``PullRequest.labels`` and ``Issue.labels`` are stored as JSON-encoded
    strings. A malformed or empty list yields an empty string so the bundle
    line stays clean.
    """
    try:
        labels = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(labels, list) or not labels:
        return ""
    return "  [" + ", ".join(str(label) for label in labels) + "]"


def _indent_block(text: str, prefix: str) -> str:
    """Indent every line of ``text`` by ``prefix``."""
    return "\n".join(prefix + line for line in text.splitlines())


def _format_commit(commit: Commit) -> list[str]:
    """Render one commit as the lines of an evidence block.

    The ``llm_description`` diff summary and a blank ``body`` are omitted so
    the bundle only carries narratives that exist.
    """
    lines = [
        f"COMMIT {_short_sha(commit.sha)}  {commit.committed_at}  "
        f"by {commit.author_name}"
    ]
    if commit.llm_description:
        lines.append(f"  Summary: {commit.llm_description}")
    lines.append(f"  Subject: {commit.subject}")
    if commit.body.strip():
        lines.append("  Body:")
        lines.append(_indent_block(commit.body.strip(), "    "))
    return lines


def _format_pr(pr: PullRequest) -> list[str]:
    """Render one pull request as the indented lines of an evidence block."""
    when = f"merged {pr.merged_at}" if pr.merged_at else pr.state
    author = f"by {pr.author}" if pr.author else "by unknown"
    lines = [f"  PR #{pr.number}  {author}  {when}{_labels_suffix(pr.labels)}"]
    lines.append(f"    Title: {pr.title}")
    if pr.body and pr.body.strip():
        lines.append("    Body:")
        lines.append(_indent_block(pr.body.strip(), "      "))
    return lines


def _format_issue(issue: Issue) -> list[str]:
    """Render one issue as the indented lines of an evidence block."""
    author = f"by {issue.author}" if issue.author else "by unknown"
    lines = [
        f"  Issue #{issue.number}  {issue.state}  "
        f"{author}{_labels_suffix(issue.labels)}"
    ]
    lines.append(f"    Title: {issue.title}")
    if issue.body and issue.body.strip():
        lines.append("    Body:")
        lines.append(_indent_block(issue.body.strip(), "      "))
    return lines


def _format_evidence(evidence: Sequence[CommitEvidence]) -> str:
    """Render an evidence bundle as the text payload for the rationale prompt.

    Commits are formatted in the order given — the caller controls
    ordering. Each commit block is followed by its linked pull requests and
    issues. The JSON-encoded ``labels`` columns are decoded here.
    """
    n_prs = sum(len(item.pull_requests) for item in evidence)
    n_issues = sum(len(item.issues) for item in evidence)
    blocks = [
        f"Evidence: {len(evidence)} commit(s), {n_prs} PR(s), "
        f"{n_issues} issue(s)."
    ]
    for item in evidence:
        lines = _format_commit(item.commit)
        for pr in item.pull_requests:
            lines.append("")
            lines.extend(_format_pr(pr))
        for issue in item.issues:
            lines.append("")
            lines.extend(_format_issue(issue))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# Cap on the target docstring excerpt — keeps the structural section from
# crowding out the change-history evidence.
_DOCSTRING_EXCERPT_CHARS = 240


def _first_paragraph(text: str, limit: int = _DOCSTRING_EXCERPT_CHARS) -> str:
    """Return ``text``'s first paragraph, whitespace-collapsed and clipped.

    Splits on the first blank line, collapses runs of whitespace to single
    spaces, and truncates to ``limit`` characters with an ellipsis marker.
    """
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    if len(paragraph) > limit:
        return paragraph[:limit].rstrip() + "…"
    return paragraph


def _format_relation(relation: Relation) -> str:
    """Render one caller/callee :class:`Relation` as a single bullet line.

    Falls back to the neighbour symbol's own start line when the edge did not
    record a site line.
    """
    symbol = relation.symbol
    line = relation.line if relation.line is not None else symbol.start_line
    return f"  - {symbol.qualified_name} ({symbol.kind})  {symbol.file_path}:{line}"


def _format_relations(header: str, relations: Sequence[Relation]) -> list[str]:
    """Render a labelled caller/callee block — ``header`` plus one line each.

    An empty ``relations`` yields a single ``(none recorded)`` line so the
    block's meaning stays explicit.
    """
    lines = [header]
    if relations:
        lines.extend(_format_relation(r) for r in relations)
    else:
        lines.append("  (none recorded)")
    return lines


def _format_symbol_context(context: SymbolContext) -> str:
    """Render a :class:`SymbolContext` as the structural-evidence section.

    The block names the target symbol — kind, location, signature, and a
    docstring excerpt — then lists its callers (fan-in) and callees (fan-out).
    It is prepended to the change-history evidence so the model sees *what the
    code is* before *how it got there*.
    """
    target = context.target
    lines = [
        "CODE GRAPH CONTEXT",
        "",
        f"Target: {target.qualified_name} ({target.kind})",
        f"  {target.file_path}:{target.start_line}-{target.end_line}",
    ]
    if target.signature and target.signature.strip():
        # CodeGraph stores signatures verbatim — often multi-line; collapse to
        # one line so the block's indentation stays consistent.
        lines.append(f"  {' '.join(target.signature.split())}")
    if target.docstring and target.docstring.strip():
        lines.append(f"  {_first_paragraph(target.docstring)}")
    lines.append("")
    lines.extend(
        _format_relations(
            f"Called by ({len(context.callers)} caller(s) — "
            "blast radius of a change):",
            context.callers,
        )
    )
    lines.append("")
    lines.extend(
        _format_relations(
            f"Calls ({len(context.callees)} callee(s) — what this depends on):",
            context.callees,
        )
    )
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """Strip a single wrapping Markdown code fence, if present.

    The rationale system prompt asks for raw JSON, but models still wrap
    output in a ``json`` code fence. A leading fence line and a trailing
    fence are removed; text without a fence is returned unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]  # drop the opening ``` / ```json line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_rationale_json(text: str) -> dict:
    """Parse and validate the model's rationale output.

    Strips an optional Markdown code fence, decodes the JSON, and checks the
    five-key schema: ``purpose`` and ``why`` are strings; ``constraints``,
    ``tradeoffs`` and ``risks`` are lists of strings. Unknown keys are
    ignored.

    Parameters
    ----------
    text : str
        The raw assistant text from :attr:`CompletionResponse.text`.

    Returns
    -------
    dict
        The validated payload — the five schema keys plus any extras.

    Raises
    ------
    RationaleError
        If the text is not a JSON object, or any required key is missing or
        has the wrong type.
    """
    payload = _strip_code_fence(text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RationaleError(
            f"rationale output is not valid JSON: {exc}; got {payload[:120]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RationaleError(
            f"rationale output must be a JSON object, got {type(parsed).__name__}"
        )
    for key in _REQUIRED_STR_FIELDS:
        if key not in parsed:
            raise RationaleError(f"rationale output missing required key {key!r}")
        if not isinstance(parsed[key], str):
            raise RationaleError(
                f"rationale key {key!r} must be a string, "
                f"got {type(parsed[key]).__name__}"
            )
    for key in _REQUIRED_LIST_FIELDS:
        if key not in parsed:
            raise RationaleError(f"rationale output missing required key {key!r}")
        value = parsed[key]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RationaleError(
                f"rationale key {key!r} must be a list of strings"
            )
    return parsed


class RationaleGenerator:
    """Turn an evidence bundle into a structured rationale card.

    The generator validates the bundle is non-empty, renders the
    ``rationale`` prompt, runs one :meth:`LlmClient.complete` call, and
    parses the model's JSON into a :class:`~whygraph.analyze.Rationale`.

    Parameters
    ----------
    client : LlmClient
        Pre-configured adapter. Inject a stub in tests; in production,
        :meth:`from_config` builds one via :class:`LlmClientFactory`.
    timeout_sec : int or None
        Per-call timeout forwarded into the :class:`CompletionRequest`.
        ``None`` (default) defers to the adapter's bound default.
    rationale_prompt : Prompt, optional
        Override the ``rationale`` prompt — the ``system`` + ``task`` pair.
        When ``None`` (default), it is resolved from markdown by the
        client's provider and model — see
        :func:`whygraph.analyze.prompt.resolve`. An explicit
        :class:`~whygraph.analyze.prompt.Prompt` skips resolution; its
        ``task`` should contain the
        :data:`~whygraph.analyze.prompt.RATIONALE_PLACEHOLDER` token. Mostly
        used in tests and one-off overrides.

    Examples
    --------
    >>> generator = RationaleGenerator.from_config(get_config().rationale)
    >>> rationale = generator.generate([CommitEvidence(commit)])
    >>> print(rationale.purpose)
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        timeout_sec: int | None = None,
        rationale_prompt: Prompt | None = None,
    ) -> None:
        self._client = client
        self._timeout_sec = timeout_sec
        self._rationale_prompt = (
            rationale_prompt
            if rationale_prompt is not None
            else resolve(
                _PROMPT_COMPONENT, "rationale", client.provider, client.model
            )
        )

    def __repr__(self) -> str:
        return f"RationaleGenerator(client={self._client!r})"

    @classmethod
    def from_config(
        cls,
        config: RationaleConfig,
        *,
        factory: LlmClientFactory | None = None,
    ) -> "RationaleGenerator":
        """Build a generator from a :class:`RationaleConfig`.

        Parameters
        ----------
        config : RationaleConfig
            Typically ``get_config().rationale``.
        factory : LlmClientFactory, optional
            Override the factory used to resolve ``config.provider`` into an
            :class:`LlmClient`. Defaults to a fresh
            :class:`LlmClientFactory` bound to the process-wide
            :class:`LlmConfig`. Inject a custom factory in tests to bind a
            stub adapter without touching global state.

        Returns
        -------
        RationaleGenerator
            A generator ready to call :meth:`generate`.

        Raises
        ------
        whygraph.services.llm.LlmError
            If ``config.provider`` is not registered with the factory.
            Propagated directly so the user sees the available providers.
        """
        factory = factory if factory is not None else LlmClientFactory()
        client = factory.make(config.provider, model=config.model)
        return cls(client, timeout_sec=config.timeout_sec)

    def generate(
        self,
        evidence: Sequence[CommitEvidence],
        *,
        symbol_context: SymbolContext | None = None,
    ) -> Rationale:
        """Generate a rationale card for one evidence bundle.

        Parameters
        ----------
        evidence : Sequence[CommitEvidence]
            The commits — with their linked pull requests and issues —
            whose history explains the code. Formatted into the prompt in
            the order given.
        symbol_context : SymbolContext, optional
            The target symbol's code-graph context — its signature,
            docstring, callers, and callees. When given, it is rendered as a
            ``CODE GRAPH CONTEXT`` section ahead of the change history, so the
            rationale is grounded in code structure as well as commit prose.
            ``None`` (default) omits the section.

        Returns
        -------
        Rationale
            The model's structured explanation plus provenance.

        Raises
        ------
        AnalyzeError
            If ``evidence`` is empty, or if the underlying
            :meth:`LlmClient.complete` raises :class:`LlmError`. The
            original exception is preserved as ``__cause__``.
        RationaleError
            If the model's output cannot be parsed as the expected
            rationale JSON. Subclasses :class:`AnalyzeError`.
        """
        if not evidence:
            raise AnalyzeError("empty evidence: nothing to explain")

        # TODO: capping bundle size belongs to the future evidence-bundle
        # builder — the generator neither truncates nor chunks its input.
        bundle = _format_evidence(evidence)
        if symbol_context is not None:
            bundle = f"{_format_symbol_context(symbol_context)}\n\n{bundle}"
        task = render(
            self._rationale_prompt.task, bundle, placeholder=RATIONALE_PLACEHOLDER
        )
        request = CompletionRequest.of(
            task,
            system=self._rationale_prompt.system,
            timeout_sec=self._timeout_sec,
        )

        try:
            response = self._client.complete(request)
        except LlmError as exc:
            raise AnalyzeError(f"LLM call failed: {exc}") from exc

        fields = _parse_rationale_json(response.text)
        return Rationale(
            purpose=fields["purpose"],
            why=fields["why"],
            constraints=tuple(fields["constraints"]),
            tradeoffs=tuple(fields["tradeoffs"]),
            risks=tuple(fields["risks"]),
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
