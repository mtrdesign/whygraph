"""Usage attribution in the portal process: the ledger's :class:`UsageSink`.

M2f-2 plan section 4.5. Every portal path that may spend on an LLM binds a
:class:`PortalUsageSink` beside its project context
(:func:`whygraph.core.context.use_project`):

==============================  ==========  ================================
Path                            Source      Bound in
==============================  ==========  ================================
Explorer data / Generate        explorer    ``project_db_access(usage_source=)``
Chat                            chat        ``project_db_access(usage_source=)``
Local ``/mcp/<slug>``           mcp         :class:`~whygraph.portal.mcp_mount.McpDispatcher`
``/api/v1`` (a linked portal)   agent       ``v1_project_access`` / ``v1_project_db_access``
==============================  ==========  ================================

(A scan child's calls are attributed by the runner, from its own spec.)

The :class:`Attribution` - who, which project, which key, which connection -
is frozen when the sink is built, so nothing is queried per call; the
:class:`~whygraph.core.usage.UsageScope` (source, chat session) is the
mutable part. On :meth:`PortalUsageSink.record` the call is priced
(:func:`price_usage`) and the finished
:class:`~whygraph.portal.usage_store.UsageRow` goes to the
:class:`~whygraph.portal.usage_store.SpendBook` and the
:class:`~whygraph.portal.usage_store.UsageWriter`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from whygraph.core.config import Config
from whygraph.core.usage import UsageRecord, UsageScope

from .prices import PriceOverrides, cost, has_custom_endpoint, price_for
from .usage_store import PriceBook, SpendBook, UsageRow, UsageWriter

if TYPE_CHECKING:
    from .deps import BoundProject, PortalState
    from .security import Principal

SYSTEM_LABEL = "System"
"""The actor label of calls no member triggered (webhook / reconcile / hook scans)."""

_SIX_DP = Decimal("0.000001")


@dataclass(frozen=True)
class Attribution:
    """Who and what a bound sink's calls are charged to (frozen at bind).

    Attributes
    ----------
    org_id : int
        The organization.
    org_slug : str
        Its slug (for audit events about the org's budgets).
    project_id : int or None
        The project.
    project_slug, project_name : str
        Snapshots.
    actor_kind : str
        ``"member"`` or ``"system"``.
    user_id : int or None
        The member; ``None`` for the system actor.
    actor_label : str
        ``"Name (@login)"`` (production), ``"Name"`` (local mode) or
        ``"System"``.
    config : Config
        The project's effective config (custom endpoints are never priced
        from the bundled table).
    key_scopes : Mapping[str, str]
        ``ProjectContext.key_scopes``: provider tag -> key scope.
    connection_id : int or None
        The connection token of a ``/api/v1`` call.
    client_name : str or None
        That connection's ``client_name`` (the machine).
    """

    org_id: int
    org_slug: str
    project_id: int | None
    project_slug: str
    project_name: str
    actor_kind: str
    user_id: int | None
    actor_label: str
    config: Config
    key_scopes: Mapping[str, str] = field(default_factory=dict)
    connection_id: int | None = None
    client_name: str | None = None


def actor_label(principal: Principal | None, *, production: bool) -> str:
    """The label a member's rows carry.

    Parameters
    ----------
    principal : Principal or None
        The request's principal.
    production : bool
        Whether the portal runs in production mode.

    Returns
    -------
    str
        ``"Name (@login)"`` in production for a GitHub account, else
        ``"Name"``; ``"Member"`` when there is no principal.
    """
    if principal is None:
        return "Member"
    name = principal.display_name
    if production and principal.github_login:
        return f"{name} (@{principal.github_login})"
    return name


@dataclass(frozen=True)
class Priced:
    """The cost of one call.

    Attributes
    ----------
    cost_usd : Decimal or None
        ``None`` when unpriced.
    cost_source : str
        ``"provider"``, ``"estimated"`` or ``"unpriced"``.
    price_version : str or None
        ``"bundled:<as_of>"`` / ``"org:<updated_at>"`` for an estimate.
    """

    cost_usd: Decimal | None
    cost_source: str
    price_version: str | None = None


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _provider_cost(value: float | None) -> Decimal | None:
    """A provider-reported cost as a 6-dp Decimal, or ``None`` if unusable."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(_SIX_DP)


def price_usage(
    rec: UsageRecord,
    *,
    provider: str,
    model_requested: str,
    overrides: PriceOverrides,
    config: Config | None,
) -> Priced:
    """Price one call (M2f-2 plan section 4.4).

    In order: the provider's own cost (``openrouter`` only, and only without
    a custom endpoint); an estimate from the org override or the bundled
    table (the requested model first, then the served one); otherwise
    unpriced. A call that reported no tokens and no provider cost is
    unpriced.

    Parameters
    ----------
    rec : UsageRecord
        The counts (its ``provider`` / ``model_requested`` are ignored in
        favour of the keywords, which a caller may take from its own spec).
    provider, model_requested : str
        The provider tag and the configured model.
    overrides : PriceOverrides
        The org's price overrides.
    config : Config or None
        The effective config, for :func:`has_custom_endpoint`; ``None``
        means no custom endpoint.

    Returns
    -------
    Priced
        The cost, its source and the price version.
    """
    custom = config is not None and has_custom_endpoint(config, provider)
    if provider == "openrouter" and not custom:
        reported = _provider_cost(rec.provider_cost_usd)
        if reported is not None:
            return Priced(reported, "provider")
    if all(getattr(rec, name) is None for name in _TOKEN_FIELDS):
        return Priced(None, "unpriced")
    models = [model_requested]
    if rec.model_served and rec.model_served != model_requested:
        models.append(rec.model_served)
    for model in models:
        hit = price_for(provider, model, overrides, custom_endpoint=custom)
        if hit is not None:
            price, version = hit
            return Priced(cost(price, rec), "estimated", version)
    return Priced(None, "unpriced")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PortalUsageSink:
    """The :class:`~whygraph.core.usage.UsageSink` of a portal request.

    Parameters
    ----------
    scope : UsageScope
        The mutable source / chat session.
    attribution : Attribution
        Frozen at bind.
    writer : UsageWriter
        Where rows are written.
    book : SpendBook
        Where spend is counted (before the write).
    prices : PriceBook
        The org price overrides, read per call.
    """

    def __init__(
        self,
        scope: UsageScope,
        attribution: Attribution,
        writer: UsageWriter,
        book: SpendBook,
        prices: PriceBook,
    ) -> None:
        self.scope = scope
        self.attribution = attribution
        self._writer = writer
        self._book = book
        self._prices = prices

    def row_for(self, rec: UsageRecord) -> UsageRow:
        """Build (and price) the ledger row of one record.

        Parameters
        ----------
        rec : UsageRecord
            What the provider call reported.

        Returns
        -------
        UsageRow
            The row, attributed from :attr:`attribution` and :attr:`scope`.
        """
        a = self.attribution
        priced = price_usage(
            rec,
            provider=rec.provider,
            model_requested=rec.model_requested,
            overrides=self._prices.for_org(a.org_id),
            config=a.config,
        )
        return UsageRow(
            org_id=a.org_id,
            project_id=a.project_id,
            project_slug=a.project_slug,
            project_name=a.project_name,
            actor_kind=a.actor_kind,
            user_id=a.user_id,
            actor_label=a.actor_label,
            source=self.scope.source,
            task=rec.task,
            provider=rec.provider,
            model_requested=rec.model_requested,
            key_scope=a.key_scopes.get(rec.provider, "none"),
            cost_source=priced.cost_source,
            created_at=_now(),
            chat_session_id=self.scope.chat_session_id,
            connection_id=a.connection_id,
            client_name=a.client_name,
            subject=rec.subject,
            model_served=rec.model_served,
            input_tokens=rec.input_tokens,
            output_tokens=rec.output_tokens,
            cache_read_tokens=rec.cache_read_tokens,
            cache_write_tokens=rec.cache_write_tokens,
            reasoning_tokens=rec.reasoning_tokens,
            cost_usd=priced.cost_usd,
            price_version=priced.price_version,
            duration_ms=rec.duration_ms,
        )

    def record(self, rec: UsageRecord) -> None:
        """Count the call in the spend book, then queue its row.

        Parameters
        ----------
        rec : UsageRecord
            What the provider call reported.
        """
        row = self.row_for(rec)
        self._book.add(row)
        self._writer.submit(row)

    def blocked_scope(self) -> str | None:
        """The exhausted hard-stopped budget scope covering this binding.

        Returns
        -------
        str or None
            Always ``None`` until budgets exist (M2f-2 step 6 reads the
            budget map here).
        """
        return None


def usage_sink_for(
    state: PortalState,
    project: BoundProject,
    *,
    source: str,
    org_slug: str,
    principal: Principal | None,
    user_id: int | None,
    connection_id: int | None = None,
    client_name: str | None = None,
) -> PortalUsageSink:
    """Build the sink a request binds for ``project``.

    Parameters
    ----------
    state : PortalState
        The portal state (writer, spend book, price book, mode).
    project : BoundProject
        The bound project.
    source : str
        ``"explorer"``, ``"chat"``, ``"mcp"`` or ``"agent"``.
    org_slug : str
        The project's org slug.
    principal : Principal or None
        The request's principal (the actor label).
    user_id : int or None
        The member the calls are charged to.
    connection_id, client_name : optional
        The connection token and machine of a ``/api/v1`` call.

    Returns
    -------
    PortalUsageSink
        A sink with a fresh :class:`~whygraph.core.usage.UsageScope`.
    """
    attribution = Attribution(
        org_id=project.org_id,
        org_slug=org_slug,
        project_id=project.id,
        project_slug=project.slug,
        project_name=project.name,
        actor_kind="member",
        user_id=user_id,
        actor_label=actor_label(principal, production=state.mode == "production"),
        config=project.ctx.config,
        key_scopes=project.ctx.key_scopes,
        connection_id=connection_id,
        client_name=client_name,
    )
    return PortalUsageSink(
        UsageScope(source=source),
        attribution,
        state.usage_writer,
        state.spend,
        state.prices,
    )


__all__ = [
    "SYSTEM_LABEL",
    "Attribution",
    "PortalUsageSink",
    "Priced",
    "actor_label",
    "price_usage",
    "usage_sink_for",
]
