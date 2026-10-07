"""The budget routes: read an org's budgets, set and remove them.

M2f-2 plan sections 4.7, 4.8 and 4.11. Two routers:

- :data:`budget_router` (both modes): ``GET /api/budgets`` (``org.usage``),
  ``PUT`` / ``DELETE /api/budgets/org`` and ``PUT`` / ``DELETE
  /api/projects/{slug}/budget`` (``org.budgets``; the project route is an
  org action on a bound project, like ``DELETE /api/projects/{slug}``);
- :data:`budget_member_router` (production only, ``require_production``
  before the org dependency): ``PUT`` / ``DELETE /api/budgets/member-default``
  and ``/api/budgets/members/{uid}``.

The both-modes routes stay off ``/api/org/`` on purpose: every
``/api/org/*`` route is production-only.

**Validation.** ``0 < monthly_usd <= 1,000,000`` (two decimals); a
project, member-default or member budget must be at or below the org budget
when there is one (``422 budget_above_org``); an org budget below an
existing child is refused with the list (``409 budget_below_children``) -
never clamped. An org admin may not set or remove their own member override
or an owner's (``403 forbidden``, plan section 0.2 #12); owners may set
anyone's. Every write locks the org row first, so concurrent budget writes
of one org are serialized and validate against what is committed.

After a write the org's budget map is reloaded
(:func:`~whygraph.portal.budgets.reload_org_budgets`) and, on a ``PUT``, its
thresholds re-evaluated; ``budget_set`` / ``budget_removed`` are audited.
A ``DELETE`` is idempotent (``204`` whether or not there was a budget).

**Prices** (also on :data:`budget_router`): ``GET /api/prices``
(``org.read``) merges the bundled table with the org's overrides; ``PUT
/api/prices`` (a body: OpenRouter model ids contain ``/``) upserts an
override and ``DELETE /api/prices?provider=&model=`` reverts one
(``org.budgets``). After a write the org's
:class:`~whygraph.portal.usage_store.PriceBook` entry is reloaded and
``price_override_set`` / ``price_override_removed`` is audited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, delete, select

from whygraph.services.llm.factory import LlmClientFactory

from .audit import audit
from .authz import Action, OrgAccess, Role
from .budgets import (
    MAX_MONTHLY_USD,
    budget_scope,
    month_resets_at,
    reload_org_budgets,
)
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    current_user,
    org_access,
    portal_state,
    project_access,
    require_production,
)
from .models import (
    Budget,
    BudgetAlert,
    Membership,
    Organization,
    PriceOverride,
    Project,
    UsageEvent,
    User,
)
from .prices import Price, bundled_prices
from .security import Principal
from .usage import actor_label
from .usage_store import current_month, reload_org_prices

budget_router = APIRouter()
"""The both-modes budget routes; included before the ``/api`` 404 catch-all."""

budget_member_router = APIRouter(dependencies=[Depends(require_production)])
"""The per-member budget routes: production only (local mode has one user)."""

_CENTS = Decimal("0.01")
_SIX_DP = Decimal("0.000001")

MAX_RATE_PER_MTOK = Decimal("10000")
"""The highest price override, USD per million tokens (a typo guard)."""

__all__ = [
    "MAX_RATE_PER_MTOK",
    "BudgetBody",
    "PriceBody",
    "budget_member_router",
    "budget_router",
]


class BudgetBody(BaseModel):
    """``PUT`` body of every budget route.

    Attributes
    ----------
    monthly_usd : Decimal
        The budget per calendar month (UTC), ``> 0`` and ``<= 1,000,000``;
        rounded to cents.
    hard_stop : bool
        Turn LLM spend off for the covered scope once the budget is spent.
    """

    model_config = ConfigDict(extra="forbid")

    monthly_usd: Decimal
    hard_stop: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _amount(body: BudgetBody) -> Decimal:
    """The validated, cent-rounded amount (``422 invalid_amount`` otherwise)."""
    try:
        amount = body.monthly_usd.quantize(_CENTS)
    except (InvalidOperation, ValueError):
        amount = None
    if amount is None or not amount.is_finite() or not 0 < amount <= MAX_MONTHLY_USD:
        raise ApiError(
            422,
            "a monthly budget is more than $0 and at most $1,000,000",
            code="invalid_amount",
        )
    return amount


def _money(value: Decimal | None, places: Decimal) -> float | None:
    return None if value is None else float(Decimal(value).quantize(places))


def _pct(spent: Decimal | None, monthly: Decimal) -> float | None:
    if spent is None or not monthly:
        return None
    return round(float(spent * 100 / monthly), 1)


def _budget_dict(row: Budget, spent: Decimal | None) -> dict[str, Any]:
    """``Budget = {monthly_usd, hard_stop, spent_usd, pct}`` (plan section 4.11)."""
    return {
        "monthly_usd": _money(row.monthly_usd, _CENTS),
        "hard_stop": bool(row.hard_stop),
        "spent_usd": _money(spent, _SIX_DP),
        "pct": _pct(spent, Decimal(row.monthly_usd)),
    }


def _spent(state: PortalState, row: Budget) -> Decimal | None:
    """What a budget's target spent this month (``None`` for the member default)."""
    if row.scope == "org":
        return state.spend.spent(row.org_id)
    if row.scope == "project" and row.project_id is not None:
        return state.spend.spent(row.org_id, project_id=row.project_id)
    if row.scope == "member" and row.user_id is not None:
        return state.spend.spent(row.org_id, user_id=row.user_id)
    return None  # a member default caps each member separately


def _lock_org(db: Session, org_id: int) -> None:
    """Serialize the org's budget writes (``SELECT ... FOR UPDATE``)."""
    found = db.exec(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    ).first()
    if found is None:
        raise ApiError(404, "not found")


def _child(db: Session, row: Budget, production: bool) -> dict[str, Any]:
    """One entry of ``409 budget_below_children``'s ``children``."""
    child: dict[str, Any] = {
        "scope": row.scope,
        "monthly_usd": _money(row.monthly_usd, _CENTS),
    }
    if row.project_id is not None:
        project = db.get(Project, row.project_id)
        if project is not None:
            child.update(slug=project.slug, name=project.name)
    if row.user_id is not None:
        user = db.get(User, row.user_id)
        if user is not None:
            child.update(uid=user.uid, label=actor_label(user, production=production))  # type: ignore[arg-type]
    return child


def _write(
    state: PortalState,
    org_id: int,
    *,
    scope: str,
    amount: Decimal,
    hard_stop: bool,
    updated_by: int,
    project_id: int | None = None,
    user_id: int | None = None,
) -> Budget:
    """Validate and upsert one budget, under the org lock; returns the row."""
    production = state.mode == "production"
    with get_session() as db:
        _lock_org(db, org_id)
        rows = db.exec(select(Budget).where(Budget.org_id == org_id)).all()
        org_row = next((r for r in rows if r.scope == "org"), None)
        if scope == "org":
            below = [r for r in rows if r.scope != "org" and r.monthly_usd > amount]
            if below:
                raise ApiError(
                    409,
                    "the organization's budget cannot be lower than a project or "
                    "member budget; lower those first",
                    code="budget_below_children",
                    children=[_child(db, r, production) for r in below],
                )
        elif org_row is not None and amount > org_row.monthly_usd:
            raise ApiError(
                422,
                "a project or member budget must be at or below the "
                f"organization's (${org_row.monthly_usd})",
                code="budget_above_org",
                org_monthly_usd=_money(org_row.monthly_usd, _CENTS),
            )
        row = next(
            (
                r
                for r in rows
                if r.scope == scope
                and r.project_id == project_id
                and r.user_id == user_id
            ),
            None,
        )
        if row is None:
            row = Budget(
                org_id=org_id, scope=scope, project_id=project_id, user_id=user_id
            )
        row.monthly_usd = amount
        row.hard_stop = hard_stop
        row.updated_by = updated_by
        row.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.add(row)
        db.flush()
        db.refresh(row)
        db.expunge(row)
        return row


def _delete(
    org_id: int,
    *,
    scope: str,
    project_id: int | None = None,
    user_id: int | None = None,
) -> Budget | None:
    """Delete one budget, under the org lock; the removed row, if there was one."""
    with get_session() as db:
        _lock_org(db, org_id)
        row = db.exec(
            select(Budget).where(
                Budget.org_id == org_id,
                Budget.scope == scope,
                col(Budget.project_id).is_(None)
                if project_id is None
                else Budget.project_id == project_id,
                col(Budget.user_id).is_(None)
                if user_id is None
                else Budget.user_id == user_id,
            )
        ).first()
        if row is None:
            return None
        removed = Budget(**row.model_dump())  # a detached copy for the audit
        db.delete(row)
        return removed


def _after_write(state: PortalState, org_id: int, *, evaluate: bool) -> None:
    """Reload the org's budget map; on a ``PUT`` also re-evaluate its thresholds."""
    reload_org_budgets(state.budgets, org_id)
    if evaluate:
        state.budgets.evaluate_org(org_id)


def _audit_change(
    event: str,
    request: Request,
    principal: Principal,
    access_org: tuple[int, str],
    row: Budget,
    *,
    project: str | None = None,
    target: str | None = None,
) -> None:
    org_id, org_slug = access_org
    audit(
        event,
        request,
        uid=principal.uid,
        target=target,
        org_id=org_id,
        org=org_slug,
        scope=row.scope,
        project=project,
        monthly_usd=str(Decimal(row.monthly_usd).quantize(_CENTS)),
        hard_stop=bool(row.hard_stop),
    )


def _member_target(access: OrgAccess, uid: str) -> tuple[int, str]:
    """The org member ``uid``'s ``(users.id, role)``; ``404 not_member`` otherwise."""
    with get_session() as db:
        found = db.exec(
            select(User.id, Membership.role)
            .join(Membership, col(Membership.user_id) == col(User.id))
            .where(User.uid == uid, Membership.org_id == access.org_id)
        ).first()
    if found is None or found[0] is None:
        raise ApiError(404, "no such member of this organization", code="not_member")
    return found[0], found[1]


def _check_member_authority(access: OrgAccess, user_id: int, role: str) -> None:
    """An org admin may not set or remove their own override or an owner's."""
    if access.role is Role.OWNER:
        return
    if user_id == access.user_id or role == Role.OWNER.value:
        raise ApiError(
            403,
            "an organization admin cannot set or remove their own member budget "
            "or an owner's; ask an owner",
            code="forbidden",
        )


# ---------------------------------------------------------------------------
# GET /api/budgets
# ---------------------------------------------------------------------------


@budget_router.get("/api/budgets")
def get_budgets(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_USAGE)),
) -> dict[str, Any]:
    """The org's budgets, this month's spend against them and its alerts.

    Returns
    -------
    dict
        ``{month, resets_at, org, member_default, members: [Budget + {uid,
        label}], projects: [Budget + {slug, name}], alerts: [{scope, label,
        threshold, crossed_at, spent_usd}], unpriced_calls}`` where
        ``Budget = {monthly_usd, hard_stop, spent_usd, pct}``. The member
        default's ``spent_usd`` / ``pct`` are ``null`` (it caps each member
        separately). Local mode has no member parts.
    """
    state = portal_state(request)
    production = state.mode == "production"
    month = current_month()
    with get_session() as db:
        rows = db.exec(
            select(Budget).where(Budget.org_id == access.org_id).order_by(Budget.id)
        ).all()
        projects = {
            p.id: p
            for p in db.exec(
                select(Project).where(
                    col(Project.id).in_([r.project_id for r in rows if r.project_id])
                )
            ).all()
        }
        alert_rows = db.exec(
            select(BudgetAlert, Budget)
            .join(Budget, col(Budget.id) == col(BudgetAlert.budget_id))
            .where(Budget.org_id == access.org_id, BudgetAlert.month == month)
            .order_by(col(BudgetAlert.crossed_at), col(BudgetAlert.id))
        ).all()
        user_ids = {r.user_id for r in rows if r.user_id is not None}
        user_ids |= {a.user_id for a, _ in alert_rows if a.user_id is not None}
        users = {
            u.id: u
            for u in db.exec(select(User).where(col(User.id).in_(user_ids))).all()
        }
        unpriced = db.exec(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.org_id == access.org_id,
                col(UsageEvent.created_at) >= f"{month}-01T00:00:00+00:00",
                UsageEvent.cost_source == "unpriced",
            )
        ).one()
        db.expunge_all()  # read after the session closes

    def label_of(user_id: int | None) -> str | None:
        user = users.get(user_id) if user_id is not None else None
        return None if user is None else actor_label(user, production=production)  # type: ignore[arg-type]

    body: dict[str, Any] = {
        "month": month,
        "resets_at": month_resets_at(month),
        "org": None,
        "member_default": None,
        "members": [],
        "projects": [],
        "alerts": [],
        "unpriced_calls": int(unpriced or 0),
    }
    for row in rows:
        entry = _budget_dict(row, _spent(state, row))
        if row.scope == "org":
            body["org"] = entry
        elif row.scope == "project":
            project = projects.get(row.project_id)
            if project is not None:
                body["projects"].append(
                    {**entry, "slug": project.slug, "name": project.name}
                )
        elif not production:
            continue  # member budgets are production-only
        elif row.scope == "member_default":
            body["member_default"] = entry
        elif row.scope == "member":
            user = users.get(row.user_id)
            if user is not None:
                body["members"].append(
                    {**entry, "uid": user.uid, "label": label_of(row.user_id)}
                )
    for alert, budget in alert_rows:
        if budget.scope == "org":
            label = access.org_name
        elif budget.scope == "project":
            project = projects.get(budget.project_id)
            label = project.name if project is not None else None
        else:
            label = label_of(alert.user_id or budget.user_id)
        body["alerts"].append(
            {
                "scope": budget_scope(budget.scope),
                "label": label,
                "threshold": alert.threshold,
                "crossed_at": alert.crossed_at,
                "spent_usd": _money(alert.spent_usd, _SIX_DP),
            }
        )
    return body


# ---------------------------------------------------------------------------
# The org budget (both modes)
# ---------------------------------------------------------------------------


@budget_router.put("/api/budgets/org")
def put_org_budget(
    body: BudgetBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    """Set the org budget; ``409 budget_below_children`` below a child budget."""
    state = portal_state(request)
    row = _write(
        state,
        access.org_id,
        scope="org",
        amount=_amount(body),
        hard_stop=body.hard_stop,
        updated_by=principal.user_id,
    )
    _after_write(state, access.org_id, evaluate=True)
    _audit_change(
        "budget_set", request, principal, (access.org_id, access.org_slug), row
    )
    return _budget_dict(row, _spent(state, row))


@budget_router.delete("/api/budgets/org", status_code=204)
def delete_org_budget(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove the org budget (``204``, also when there was none)."""
    state = portal_state(request)
    row = _delete(access.org_id, scope="org")
    if row is not None:
        _after_write(state, access.org_id, evaluate=False)
        _audit_change(
            "budget_removed", request, principal, (access.org_id, access.org_slug), row
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# A project's budget (both modes)
# ---------------------------------------------------------------------------


@budget_router.put("/api/projects/{slug}/budget")
def put_project_budget(
    body: BudgetBody,
    request: Request,
    project: BoundProject = Depends(project_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    """Set a project's budget; ``422 budget_above_org`` above the org budget."""
    state = portal_state(request)
    row = _write(
        state,
        project.org_id,
        scope="project",
        amount=_amount(body),
        hard_stop=body.hard_stop,
        updated_by=principal.user_id,
        project_id=project.id,
    )
    _after_write(state, project.org_id, evaluate=True)
    org = (project.org_id, request.scope.get("state", {}).get("org_slug") or "")
    _audit_change("budget_set", request, principal, org, row, project=project.slug)
    return {
        **_budget_dict(row, _spent(state, row)),
        "slug": project.slug,
        "name": project.name,
    }


@budget_router.delete("/api/projects/{slug}/budget", status_code=204)
def delete_project_budget(
    request: Request,
    project: BoundProject = Depends(project_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove a project's budget (``204``, also when there was none)."""
    state = portal_state(request)
    row = _delete(project.org_id, scope="project", project_id=project.id)
    if row is not None:
        _after_write(state, project.org_id, evaluate=False)
        org = (project.org_id, request.scope.get("state", {}).get("org_slug") or "")
        _audit_change(
            "budget_removed", request, principal, org, row, project=project.slug
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Member budgets (production only)
# ---------------------------------------------------------------------------


@budget_member_router.put("/api/budgets/member-default")
def put_member_default(
    body: BudgetBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    """Set the org-wide member default; ``422 budget_above_org`` above the org's."""
    state = portal_state(request)
    row = _write(
        state,
        access.org_id,
        scope="member_default",
        amount=_amount(body),
        hard_stop=body.hard_stop,
        updated_by=principal.user_id,
    )
    _after_write(state, access.org_id, evaluate=True)
    _audit_change(
        "budget_set", request, principal, (access.org_id, access.org_slug), row
    )
    return _budget_dict(row, None)


@budget_member_router.delete("/api/budgets/member-default", status_code=204)
def delete_member_default(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove the member default (``204``, also when there was none)."""
    state = portal_state(request)
    row = _delete(access.org_id, scope="member_default")
    if row is not None:
        _after_write(state, access.org_id, evaluate=False)
        _audit_change(
            "budget_removed", request, principal, (access.org_id, access.org_slug), row
        )
    return Response(status_code=204)


@budget_member_router.put("/api/budgets/members/{uid}")
def put_member_budget(
    uid: str,
    body: BudgetBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    """Set one member's override.

    ``404 not_member``; ``403 forbidden`` for an org admin's own override or
    an owner's; ``422 budget_above_org`` above the org budget.
    """
    state = portal_state(request)
    amount = _amount(body)
    user_id, role = _member_target(access, uid)
    _check_member_authority(access, user_id, role)
    row = _write(
        state,
        access.org_id,
        scope="member",
        amount=amount,
        hard_stop=body.hard_stop,
        updated_by=principal.user_id,
        user_id=user_id,
    )
    _after_write(state, access.org_id, evaluate=True)
    _audit_change(
        "budget_set",
        request,
        principal,
        (access.org_id, access.org_slug),
        row,
        target=uid,
    )
    return {**_budget_dict(row, _spent(state, row)), "uid": uid}


@budget_member_router.delete("/api/budgets/members/{uid}", status_code=204)
def delete_member_budget(
    uid: str,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove one member's override (``204``, also when there was none).

    ``404 not_member``; ``403 forbidden`` as for the ``PUT``.
    """
    state = portal_state(request)
    user_id, role = _member_target(access, uid)
    _check_member_authority(access, user_id, role)
    row = _delete(access.org_id, scope="member", user_id=user_id)
    if row is not None:
        _after_write(state, access.org_id, evaluate=False)
        _audit_change(
            "budget_removed",
            request,
            principal,
            (access.org_id, access.org_slug),
            row,
            target=uid,
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Prices (both modes)
# ---------------------------------------------------------------------------


class PriceBody(BaseModel):
    """``PUT /api/prices`` body: one org override, USD per million tokens.

    Attributes
    ----------
    provider : str
        A built-in provider tag.
    model : str
        The model id exactly as configured or served (OpenRouter's
        ``vendor/model`` included).
    input_per_mtok, output_per_mtok : Decimal
        Input and output rates, ``0 <= rate <= 10,000``.
    cache_read_per_mtok, cache_write_per_mtok : Decimal or None
        Cache rates; ``None`` means the input rate.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(max_length=40)
    model: str = Field(max_length=200)
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None


def _rate(value: Decimal | None, name: str) -> Decimal | None:
    """A validated rate, rounded to 6 places (``422 invalid_price`` otherwise)."""
    if value is None:
        return None
    try:
        rate = value.quantize(_SIX_DP)
    except (InvalidOperation, ValueError):
        rate = None
    if rate is None or not rate.is_finite() or not 0 <= rate <= MAX_RATE_PER_MTOK:
        raise ApiError(
            422,
            f"{name} is a price per million tokens from $0 to $10,000",
            code="invalid_price",
            field=name,
        )
    return rate


def _price_target(provider: str, model: str) -> tuple[str, str]:
    """A checked ``(provider, model)``; ``422 bad_provider`` / ``bad_model``."""
    provider, model = provider.strip(), model.strip()
    if provider not in LlmClientFactory.BUILTIN_PROVIDERS:
        raise ApiError(
            422,
            "provider is one of " + ", ".join(LlmClientFactory.BUILTIN_PROVIDERS),
            code="bad_provider",
        )
    if not model or any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise ApiError(422, "model must be a model id", code="bad_model")
    return provider, model


def _rate_out(value: Decimal | None) -> float | None:
    return None if value is None else float(Decimal(value).quantize(_SIX_DP))


def _price_row(
    provider: str, model: str, price: Price, origin: str, updated_at: str | None
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "input_per_mtok": _rate_out(price.input),
        "output_per_mtok": _rate_out(price.output),
        "cache_read_per_mtok": _rate_out(price.cache_read),
        "cache_write_per_mtok": _rate_out(price.cache_write),
        "origin": origin,
        "updated_at": updated_at,
    }


@budget_router.get("/api/prices")
def get_prices(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
) -> dict[str, Any]:
    """The org's price table: the bundled rows with its overrides on top.

    Returns
    -------
    dict
        ``{as_of, rows: [{provider, model, input_per_mtok,
        output_per_mtok, cache_read_per_mtok, cache_write_per_mtok,
        origin: "bundled" | "override", updated_at}]}`` sorted by provider
        and model; a bundled row's ``updated_at`` is ``null`` (``as_of``
        dates the whole table).
    """
    state = portal_state(request)
    table = bundled_prices()
    rows = {
        key: _price_row(*key, price, "bundled", None)
        for key, price in table.rows.items()
    }
    for key, (price, updated_at) in state.prices.for_org(access.org_id).items():
        rows[key] = _price_row(*key, price, "override", updated_at)
    return {"as_of": table.as_of, "rows": [rows[key] for key in sorted(rows)]}


@budget_router.put("/api/prices")
def put_price(
    body: PriceBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    """Set (or replace) the org's price of one model.

    ``422 bad_provider`` / ``bad_model`` / ``invalid_price`` (with
    ``field``). New calls are priced with it at once; recorded calls keep
    the cost they were written with.
    """
    state = portal_state(request)
    provider, model = _price_target(body.provider, body.model)
    rates = {
        name: _rate(getattr(body, name), name)
        for name in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_read_per_mtok",
            "cache_write_per_mtok",
        )
    }
    if rates["input_per_mtok"] is None or rates["output_per_mtok"] is None:
        raise ApiError(422, "input and output rates are required", code="invalid_price")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = {**rates, "updated_by": principal.user_id, "updated_at": now}
    with get_session() as db:
        db.exec(  # type: ignore[call-overload]
            pg_insert(PriceOverride)
            .values(org_id=access.org_id, provider=provider, model=model, **values)
            .on_conflict_do_update(constraint="pk_price_overrides", set_=values)
        )
    reload_org_prices(state.prices, access.org_id)
    audit(
        "price_override_set",
        request,
        uid=principal.uid,
        org_id=access.org_id,
        org=access.org_slug,
        provider=provider,
        model=model,
        **{name: None if rate is None else str(rate) for name, rate in rates.items()},
    )
    price = Price(
        input=rates["input_per_mtok"],
        output=rates["output_per_mtok"],
        cache_read=rates["cache_read_per_mtok"],
        cache_write=rates["cache_write_per_mtok"],
    )
    return _price_row(provider, model, price, "override", now)


@budget_router.delete("/api/prices", status_code=204)
def delete_price(
    request: Request,
    provider: str = Query(max_length=40),
    model: str = Query(max_length=200),
    access: OrgAccess = Depends(org_access(Action.ORG_BUDGETS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Revert one model to the bundled price (``204``, also when not overridden)."""
    state = portal_state(request)
    provider, model = provider.strip(), model.strip()
    with get_session() as db:
        removed = db.exec(  # type: ignore[call-overload]
            delete(PriceOverride).where(
                col(PriceOverride.org_id) == access.org_id,
                col(PriceOverride.provider) == provider,
                col(PriceOverride.model) == model,
            )
        ).rowcount
    if removed:
        reload_org_prices(state.prices, access.org_id)
        audit(
            "price_override_removed",
            request,
            uid=principal.uid,
            org_id=access.org_id,
            org=access.org_slug,
            provider=provider,
            model=model,
        )
    return Response(status_code=204)
