"""Usage-recording seam: where a metered LLM call reports what it spent.

Every successful provider round trip in the package - a commit
description chunk or synthesis call, a rationale card, a chat round - is
reported here by the function that made it, right after the provider
returned and **before** anything parses the answer (a reply that fails to
parse is still a billed call). Where the report goes is decided by the
caller's execution context, not by the metered code: a :class:`UsageSink`
bound with :func:`use_usage_sink` receives a :class:`UsageRecord` per call.

- In the portal process the sink is the usage ledger
  (:class:`whygraph.portal.usage.PortalUsageSink`), bound per request
  beside :func:`whygraph.core.context.use_project`.
- In a scan child the portal manages, the sink turns each record into a
  ``usage`` event on the JSON progress stream.
- Unbound (a headless ``whygraph scan``, a test) every function here is a
  no-op. Strict mode does not apply: spending without a sink is allowed.

The binding lives in a :class:`contextvars.ContextVar`, like the project
context: it reaches anything that runs a copy of the current context (the
analyze pool copies it per submit, anyio's worker threads copy it), but
not a bare thread.

The bound :class:`UsageScope` is deliberately **mutable**. Starlette runs
each ``next()`` of a streaming response body in a fresh context copy, so a
binding made inside the body would be lost at the next ``yield``; the
portal binds the sink once in the request task and the chat body fills in
its session id with :func:`set_scope_field`, which every copy sees.

Recording never raises into the metered code: a sink error is logged and
swallowed, the way the audit log's ``audit()`` behaves.

Examples
--------
>>> started = time.monotonic()                                  # doctest: +SKIP
>>> response = client.complete(request)                         # doctest: +SKIP
>>> record_usage("analyze", response, provider=client.provider,
...              model_requested=client.model, started=started)  # doctest: +SKIP
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Protocol

if TYPE_CHECKING:  # typing only: core never imports services at run time here
    from whygraph.services.llm.chat import TurnDone
    from whygraph.services.llm.types import CompletionResponse

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageRecord:
    """What one provider round trip reported.

    Counts only: never a prompt, a completion or chat text.

    Attributes
    ----------
    task : str
        ``"analyze"``, ``"rationale"`` or ``"chat"``.
    provider : str
        The adapter's provider tag (``client.provider``).
    model_requested : str
        The model the call asked for (``client.model``).
    model_served : str or None
        The model the provider says it served, when it said.
    input_tokens : int or None
        Every prompt token, cache reads and writes included; ``None`` when
        not reported.
    output_tokens : int or None
        Completion tokens, reasoning included.
    cache_read_tokens, cache_write_tokens : int or None
        Subsets of ``input_tokens``.
    reasoning_tokens : int or None
        A subset of ``output_tokens``.
    provider_cost_usd : float or None
        The cost the provider itself reported (only OpenRouter does).
    subject : str or None
        What the call was about: a commit SHA, a file path or a qualified
        name. The sink caps and redacts it.
    duration_ms : int or None
        How long the provider call took, when the caller timed it.
    """

    task: str
    provider: str
    model_requested: str
    model_served: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    provider_cost_usd: float | None
    subject: str | None
    duration_ms: int | None


@dataclass
class UsageScope:
    """The mutable part of a binding: where the calls come from.

    Attributes
    ----------
    source : str
        ``"scan"``, ``"explorer"``, ``"chat"``, ``"mcp"`` or ``"agent"``.
    chat_session_id : int or None
        The chat session a chat request serves, set by the chat body
        through :func:`set_scope_field`.
    """

    source: str
    chat_session_id: int | None = None


class UsageSink(Protocol):
    """Where :func:`record_usage` sends a :class:`UsageRecord`.

    Attributes
    ----------
    scope : UsageScope
        The binding's mutable scope (:func:`set_scope_field` writes it).
    """

    scope: UsageScope

    def record(self, rec: UsageRecord) -> None:
        """Take one record; may raise (the caller swallows and logs)."""
        ...

    def blocked_scope(self) -> str | None:
        """The exhausted hard-stopped budget scope, if any.

        ``"org"``, ``"project"`` or ``"member"``; ``None`` when spending
        may continue.
        """
        ...


BUDGET_EXCEEDED = "budget_exceeded"
"""The refusal reason (and HTTP error code) of an exhausted hard-stopped budget."""


class UsageBlocked(RuntimeError):
    """LLM spend is refused: an exhausted hard-stopped budget covers the caller.

    Raised before anything is spent (the chat route raises it before it
    streams, M2f-2 plan section 4.7); the portal answers it with
    ``403 {"error", "code": "budget_exceeded", "scope"}``.

    Parameters
    ----------
    scope : str or None
        Which budget is exhausted: ``"org"``, ``"project"`` or ``"member"``.
    reason : str, optional
        Why spend is refused (:data:`BUDGET_EXCEEDED`).
    message : str, optional
        The human-readable message; a default names the scope.

    Attributes
    ----------
    scope : str or None
        As passed.
    reason : str
        As passed.
    """

    def __init__(
        self,
        scope: str | None,
        *,
        reason: str = BUDGET_EXCEEDED,
        message: str | None = None,
    ) -> None:
        super().__init__(message or budget_message(scope))
        self.scope = scope
        self.reason = reason


_SCOPE_WORDS = {
    "org": "this organization",
    "project": "this project",
    "member": "your account",
}


def budget_message(scope: str | None) -> str:
    """The message of a budget refusal for ``scope``.

    Parameters
    ----------
    scope : str or None
        ``"org"``, ``"project"``, ``"member"`` or ``None``.

    Returns
    -------
    str
        E.g. "the monthly LLM budget of this project is reached; you can
        still read everything that's already generated".
    """
    whose = _SCOPE_WORDS.get(scope or "", "this organization, project or account")
    return (
        f"the monthly LLM budget of {whose} is reached; you can still read "
        "everything that's already generated"
    )


_sink: ContextVar[UsageSink | None] = ContextVar("whygraph_usage_sink", default=None)


@contextlib.contextmanager
def use_usage_sink(sink: UsageSink) -> Iterator[UsageSink]:
    """Bind ``sink`` for the duration of a block.

    Blocks nest: leaving an inner block restores the outer binding.

    Parameters
    ----------
    sink : UsageSink
        Receives every :func:`record_usage` inside the block.

    Yields
    ------
    UsageSink
        ``sink`` itself.
    """
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)


def current_usage_sink() -> UsageSink | None:
    """Return the bound sink, or ``None`` when there is none.

    Returns
    -------
    UsageSink or None
        The sink bound by the innermost enclosing :func:`use_usage_sink`.
    """
    return _sink.get()


def record_usage(
    task: str,
    response: CompletionResponse | TurnDone,
    *,
    provider: str,
    model_requested: str,
    subject: str | None = None,
    started: float | None = None,
) -> None:
    """Report one successful provider call to the bound sink.

    Call it immediately after the provider returned, before parsing.
    A no-op when no sink is bound; never raises.

    Parameters
    ----------
    task : str
        ``"analyze"``, ``"rationale"`` or ``"chat"``.
    response : CompletionResponse or TurnDone
        What the provider returned (its token fields, served ``model`` and
        ``cost_usd`` are read).
    provider : str
        The adapter's provider tag (``client.provider``).
    model_requested : str
        The model the call asked for (``client.model``).
    subject : str, optional
        What the call was about (a SHA, a path, a qualified name).
    started : float, optional
        :func:`time.monotonic` taken just before the call; gives
        ``duration_ms``.
    """
    sink = _sink.get()
    if sink is None:
        return
    try:
        duration = (
            None
            if started is None
            else max(int((time.monotonic() - started) * 1000), 0)
        )
        rec = UsageRecord(
            task=task,
            provider=provider,
            model_requested=model_requested,
            model_served=getattr(response, "model", None) or None,
            input_tokens=getattr(response, "input_tokens", None),
            output_tokens=getattr(response, "output_tokens", None),
            cache_read_tokens=getattr(response, "cache_read_tokens", None),
            cache_write_tokens=getattr(response, "cache_write_tokens", None),
            reasoning_tokens=getattr(response, "reasoning_tokens", None),
            provider_cost_usd=getattr(response, "cost_usd", None),
            subject=subject,
            duration_ms=duration,
        )
        sink.record(rec)
    except Exception:  # noqa: BLE001 -- recording must never fail the call
        _log.exception("could not record LLM usage for a %s call", task)


def usage_blocked() -> str | None:
    """The bound sink's blocked budget scope, if spending must stop.

    Returns
    -------
    str or None
        ``"org"``, ``"project"`` or ``"member"`` when an exhausted
        hard-stopped budget covers the binding; ``None`` when unbound, when
        nothing is exhausted, or when the sink fails to answer (logged).
    """
    sink = _sink.get()
    if sink is None:
        return None
    try:
        return sink.blocked_scope()
    except Exception:  # noqa: BLE001 -- never fail the caller over a check
        _log.exception("could not read the usage budget state")
        return None


_SCOPE_FIELDS = frozenset(f.name for f in dataclasses.fields(UsageScope))


def set_scope_field(**fields: object) -> None:
    """Set fields of the bound :class:`UsageScope`; a no-op when unbound.

    The scope object is shared by every context copy of the request, so a
    value set here is seen by later ``next()`` calls of a streaming body.

    Parameters
    ----------
    **fields
        :class:`UsageScope` attribute names and their new values.

    Raises
    ------
    TypeError
        For a name :class:`UsageScope` does not have (a programming error).
    """
    unknown = set(fields) - _SCOPE_FIELDS
    if unknown:
        raise TypeError(f"UsageScope has no field(s) {sorted(unknown)}")
    sink = _sink.get()
    if sink is None:
        return
    for name, value in fields.items():
        setattr(sink.scope, name, value)


__all__ = [
    "BUDGET_EXCEEDED",
    "UsageBlocked",
    "UsageRecord",
    "UsageScope",
    "UsageSink",
    "budget_message",
    "current_usage_sink",
    "record_usage",
    "set_scope_field",
    "usage_blocked",
    "use_usage_sink",
]
