"""Monthly LLM budgets: the in-memory budget map, the hard stop, the alerts.

M2f-2 plan sections 4.7 and 4.8. An org may cap its month-to-date LLM
spend (calendar months, UTC) at four levels, one ``budgets`` row each:

- ``org`` - everything the org spends;
- ``project`` - one project's spend;
- ``member_default`` - each member's own spend, unless the member has
- ``member`` - a per-person override (production only, like the routes that
  set it).

A call counts against **every** budget that covers it: the org's, its
project's, and its member's override or else the member default. Only
priced calls count (an ``unpriced`` row adds nothing to the
:class:`~whygraph.portal.usage_store.SpendBook`).

A budget with ``hard_stop`` set turns LLM spend off for everyone it covers
once it is exhausted: :meth:`BudgetBook.blocked_scope` answers ``"member"``,
``"project"`` or ``"org"`` (in that order), and every spending surface
refuses with ``403 budget_exceeded`` and that scope. The map is read on
every bind, chat round and scan ``usage`` event, so it lives in memory
(:class:`BudgetBook`): loaded at start-up (:func:`seed_budgets`), and
reloaded per org (:func:`reload_org_budgets`) by every budget write, by a
member's removal or leave (their override goes with the membership) and by a
project's or the org's deletion.

**Alerts.** Each covering budget fires once per threshold (50 / 75 / 100%)
per month - per member, for the member default. :meth:`BudgetBook.on_add`
is the spend book's add hook; a newly crossed threshold goes into the
``fired`` set (seeded from ``budget_alerts`` at start-up) and is recorded off
the request path, on the usage writer's thread (:func:`record_alert`): one
``budget_alerts`` row (``ON CONFLICT DO NOTHING``) and, only when that row
is new, the audit event ``budget_threshold_crossed`` (plus
``budget_hard_stop_engaged`` at 100% with a hard stop). A budget ``PUT``
re-evaluates the org (:meth:`BudgetBook.evaluate_org`): lowering a budget
can cross a threshold with no new spend. Raising one does not re-arm a
threshold already fired this month; a deleted and re-created budget is a
new row and can fire again.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import col, select

from .audit import audit
from .db import get_session
from .models import BUDGET_THRESHOLDS, Budget, BudgetAlert, Organization, Project, User
from .usage_store import SpendBook, SpendTotals, UsageRow, current_month

_log = logging.getLogger(__name__)

MAX_MONTHLY_USD = Decimal("1000000")
"""The largest monthly budget (``ck_budgets_amount``)."""

_CENTS = Decimal("0.01")

__all__ = [
    "MAX_MONTHLY_USD",
    "BudgetBook",
    "BudgetLimit",
    "BudgetState",
    "Covering",
    "OrgBudgets",
    "budget_scope",
    "load_budgets",
    "load_fired",
    "month_resets_at",
    "record_alert",
    "reload_org_budgets",
    "seed_budgets",
]


def budget_scope(scope: str) -> str:
    """The refusal / alert scope of a budget row's ``scope``.

    Parameters
    ----------
    scope : str
        ``"org"``, ``"project"``, ``"member_default"`` or ``"member"``.

    Returns
    -------
    str
        ``"org"``, ``"project"`` or ``"member"`` (a member default caps a
        member).
    """
    return "member" if scope == "member_default" else scope


def month_resets_at(month: str) -> str:
    """When a ``"YYYY-MM"`` month's budgets reset: the next month's start (UTC).

    Parameters
    ----------
    month : str
        E.g. ``"2026-10"``.

    Returns
    -------
    str
        E.g. ``"2026-11-01T00:00:00+00:00"``.
    """
    year, number = (int(part) for part in month.split("-"))
    year, number = (year + 1, 1) if number == 12 else (year, number + 1)
    return f"{year:04d}-{number:02d}-01T00:00:00+00:00"


def _usd(value: Decimal) -> str:
    """A money value for an audit field (fields are stringified)."""
    return str(value.quantize(_CENTS))


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLimit:
    """One ``budgets`` row, as the map holds it.

    Attributes
    ----------
    id : int
        ``budgets.id`` (alerts key on it).
    scope : str
        ``"org"``, ``"project"``, ``"member_default"`` or ``"member"``.
    monthly_usd : Decimal
        The budget.
    hard_stop : bool
        Whether exhausting it stops LLM spend.
    project_id, user_id : int or None
        The project (``project`` scope) or member (``member`` scope).
    """

    id: int
    scope: str
    monthly_usd: Decimal
    hard_stop: bool
    project_id: int | None = None
    user_id: int | None = None


@dataclass(frozen=True)
class OrgBudgets:
    """One org's budgets.

    Attributes
    ----------
    org_slug : str
        The org's slug (for audit events).
    org : BudgetLimit or None
        The org budget.
    member_default : BudgetLimit or None
        The org-wide member default.
    members : Mapping[int, BudgetLimit]
        Per-member overrides by ``users.id``.
    projects : Mapping[int, BudgetLimit]
        Project budgets by ``projects.id``.
    """

    org_slug: str
    org: BudgetLimit | None = None
    member_default: BudgetLimit | None = None
    members: Mapping[int, BudgetLimit] = field(default_factory=dict)
    projects: Mapping[int, BudgetLimit] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Whether the org has no budget at all."""
        return (
            self.org is None
            and self.member_default is None
            and not self.members
            and not self.projects
        )


@dataclass(frozen=True)
class Covering:
    """A budget that covers a call, with its month-to-date spend.

    Attributes
    ----------
    scope : str
        ``"member"``, ``"project"`` or ``"org"``.
    budget : BudgetLimit
        The budget row (a member default for a member without override).
    spent : Decimal
        What the budget's target spent this month.
    """

    scope: str
    budget: BudgetLimit
    spent: Decimal

    @property
    def exhausted(self) -> bool:
        """Whether the month's spend reached the budget."""
        return self.spent >= self.budget.monthly_usd


@dataclass(frozen=True)
class BudgetState:
    """Where a caller stands against the budgets covering it.

    Attributes
    ----------
    spent : Mapping[str, Decimal]
        Month-to-date spend per covering scope (``"member"``,
        ``"project"``, ``"org"``).
    limits : Mapping[str, BudgetLimit]
        The covering budget per scope.
    blocked_scope : str or None
        The first exhausted hard-stopped scope, in the order member,
        project, org; ``None`` when spending may continue.
    """

    spent: Mapping[str, Decimal]
    limits: Mapping[str, BudgetLimit]
    blocked_scope: str | None


@dataclass(frozen=True)
class _Alert:
    """A threshold crossing to record (on the writer thread)."""

    org_id: int
    org_slug: str
    budget: BudgetLimit
    user_id: int | None  # the member a member default was crossed by
    month: str
    threshold: int
    spent: Decimal
    crossed_at: str


class BudgetBook:
    """Every org's budgets in memory, the hard stop and the alert trigger.

    Parameters
    ----------
    spend : SpendBook
        The month-to-date spend the budgets are measured against.
    defer : callable, optional
        Runs an alert's recording off the request path: the portal passes
        :meth:`~whygraph.portal.usage_store.UsageWriter.call`. ``None``
        records nothing (tests of the map alone).
    """

    def __init__(
        self,
        spend: SpendBook,
        *,
        defer: Callable[[Callable[[], None]], object] | None = None,
    ) -> None:
        self._spend = spend
        self._defer = defer
        self._lock = threading.Lock()
        self._orgs: dict[int, OrgBudgets] = {}
        self._fired: dict[tuple[int, int | None, str], set[int]] = {}

    # -- the map ---------------------------------------------------------

    def replace_all(self, by_org: Mapping[int, OrgBudgets]) -> None:
        """Replace every org's budgets.

        Parameters
        ----------
        by_org : Mapping[int, OrgBudgets]
            The budgets per org id (an org without any may be absent).
        """
        with self._lock:
            self._orgs = {k: v for k, v in by_org.items() if not v.empty}

    def set_org(self, org_id: int, budgets: OrgBudgets | None) -> None:
        """Replace one org's budgets (``None`` or empty: it has none).

        Fired thresholds of budgets the org no longer has are forgotten.

        Parameters
        ----------
        org_id : int
            The organization.
        budgets : OrgBudgets or None
            Its budgets now.
        """
        with self._lock:
            old = self._orgs.get(org_id)
            if budgets is None or budgets.empty:
                self._orgs.pop(org_id, None)
            else:
                self._orgs[org_id] = budgets
            if old is not None:
                kept = set() if budgets is None else {b.id for b in _all(budgets)}
                gone = {b.id for b in _all(old)} - kept
                for key in [k for k in self._fired if k[0] in gone]:
                    del self._fired[key]

    def for_org(self, org_id: int) -> OrgBudgets | None:
        """The org's budgets, or ``None`` when it has none.

        Parameters
        ----------
        org_id : int
            The organization.

        Returns
        -------
        OrgBudgets or None
            The (immutable) snapshot.
        """
        with self._lock:
            return self._orgs.get(org_id)

    # -- the hard stop ---------------------------------------------------

    def covering(
        self, org_id: int, project_id: int | None, user_id: int | None
    ) -> list[Covering]:
        """The budgets a call of ``user_id`` on ``project_id`` counts against.

        Parameters
        ----------
        org_id : int
            The organization.
        project_id : int or None
            The project (``None``: no project budget applies).
        user_id : int or None
            The member (``None``, the system actor: no member budget).

        Returns
        -------
        list[Covering]
            In the order member, project, org (only those that exist).
        """
        budgets = self.for_org(org_id)
        if budgets is None:
            return []
        found: list[Covering] = []
        if user_id is not None:
            member = budgets.members.get(user_id) or budgets.member_default
            if member is not None:
                found.append(
                    Covering(
                        "member", member, self._spend.spent(org_id, user_id=user_id)
                    )
                )
        if project_id is not None:
            project = budgets.projects.get(project_id)
            if project is not None:
                found.append(
                    Covering(
                        "project",
                        project,
                        self._spend.spent(org_id, project_id=project_id),
                    )
                )
        if budgets.org is not None:
            found.append(Covering("org", budgets.org, self._spend.spent(org_id)))
        return found

    def budget_state(
        self, org_id: int, project_id: int | None, user_id: int | None
    ) -> BudgetState:
        """Spend, limits and the blocked scope of a call (O(1)).

        Parameters
        ----------
        org_id, project_id, user_id
            As for :meth:`covering`.

        Returns
        -------
        BudgetState
            ``blocked_scope`` is the first exhausted hard-stopped scope in
            the order member, project, org.
        """
        covering = self.covering(org_id, project_id, user_id)
        blocked = next(
            (c.scope for c in covering if c.budget.hard_stop and c.exhausted), None
        )
        return BudgetState(
            spent={c.scope: c.spent for c in covering},
            limits={c.scope: c.budget for c in covering},
            blocked_scope=blocked,
        )

    def blocked_scope(
        self, org_id: int, project_id: int | None, user_id: int | None
    ) -> str | None:
        """The exhausted hard-stopped scope covering a call, or ``None``.

        Parameters
        ----------
        org_id, project_id, user_id
            As for :meth:`covering`.

        Returns
        -------
        str or None
            ``"member"``, ``"project"``, ``"org"`` or ``None``.
        """
        return self.budget_state(org_id, project_id, user_id).blocked_scope

    # -- alerts ----------------------------------------------------------

    def seed_fired(self, rows: Iterable[tuple[int, int | None, str, int]]) -> None:
        """Replace the ``fired`` set with this month's ``budget_alerts``.

        Parameters
        ----------
        rows : iterable of (budget_id, user_id, month, threshold)
            The alerts already recorded.
        """
        fired: dict[tuple[int, int | None, str], set[int]] = {}
        for budget_id, user_id, month, threshold in rows:
            fired.setdefault((budget_id, user_id, month), set()).add(int(threshold))
        with self._lock:
            self._fired = fired

    def fired(self, budget_id: int, user_id: int | None, month: str) -> set[int]:
        """The thresholds already fired for a budget (and member) in ``month``.

        Parameters
        ----------
        budget_id : int
            ``budgets.id``.
        user_id : int or None
            The member, for a member default; else ``None``.
        month : str
            ``"YYYY-MM"``.

        Returns
        -------
        set[int]
            A copy.
        """
        with self._lock:
            return set(self._fired.get((budget_id, user_id, month), ()))

    def on_add(self, row: UsageRow, totals: SpendTotals) -> None:
        """The :class:`SpendBook` add hook: fire newly crossed thresholds.

        Parameters
        ----------
        row : UsageRow
            The counted call.
        totals : SpendTotals
            The month-to-date totals right after it.
        """
        budgets = self.for_org(row.org_id)
        if budgets is None:
            return
        checks: list[tuple[BudgetLimit, int | None, Decimal]] = []
        if budgets.org is not None:
            checks.append((budgets.org, None, totals.org))
        if row.project_id is not None and totals.project is not None:
            project = budgets.projects.get(row.project_id)
            if project is not None:
                checks.append((project, None, totals.project))
        if row.user_id is not None and totals.user is not None:
            member = budgets.members.get(row.user_id)
            if member is not None:
                checks.append((member, None, totals.user))
            elif budgets.member_default is not None:
                checks.append((budgets.member_default, row.user_id, totals.user))
        self._fire(row.org_id, budgets.org_slug, totals.month, checks)

    def evaluate_org(self, org_id: int) -> None:
        """Fire every threshold the org's current spend has crossed.

        Run after a budget ``PUT``: a lowered (or new) budget may already
        be crossed. Thresholds fired earlier this month are not repeated.

        Parameters
        ----------
        org_id : int
            The organization.
        """
        budgets = self.for_org(org_id)
        if budgets is None:
            return
        spend = self._spend
        month = spend.month  # a stale month reads as no spend below
        checks: list[tuple[BudgetLimit, int | None, Decimal]] = []
        if budgets.org is not None:
            checks.append((budgets.org, None, spend.spent(org_id)))
        for project_id, project in budgets.projects.items():
            checks.append((project, None, spend.spent(org_id, project_id=project_id)))
        for user_id, member in budgets.members.items():
            checks.append((member, None, spend.spent(org_id, user_id=user_id)))
        if budgets.member_default is not None:
            for user_id, amount in spend.spent_by_user(org_id).items():
                if user_id not in budgets.members:
                    checks.append((budgets.member_default, user_id, amount))
        self._fire(org_id, budgets.org_slug, month, checks)

    def _fire(
        self,
        org_id: int,
        org_slug: str,
        month: str,
        checks: list[tuple[BudgetLimit, int | None, Decimal]],
    ) -> None:
        """Mark the newly crossed thresholds fired and defer their recording."""
        crossed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        alerts: list[_Alert] = []
        with self._lock:
            for key in [k for k in self._fired if k[2] < month]:
                del self._fired[key]  # last month's: never read again
            for budget, user_id, spent in checks:
                crossed = {
                    t
                    for t in BUDGET_THRESHOLDS
                    if spent * 100 >= budget.monthly_usd * t
                }
                if not crossed:
                    continue
                fired = self._fired.setdefault((budget.id, user_id, month), set())
                for threshold in sorted(crossed - fired):
                    fired.add(threshold)
                    alerts.append(
                        _Alert(
                            org_id=org_id,
                            org_slug=org_slug,
                            budget=budget,
                            user_id=user_id,
                            month=month,
                            threshold=threshold,
                            spent=spent,
                            crossed_at=crossed_at,
                        )
                    )
        if self._defer is None:
            return
        for alert in alerts:
            self._defer(partial(record_alert, alert))


def _all(budgets: OrgBudgets) -> list[BudgetLimit]:
    """Every budget of an org."""
    found = [b for b in (budgets.org, budgets.member_default) if b is not None]
    return found + list(budgets.members.values()) + list(budgets.projects.values())


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _limit(row: Budget) -> BudgetLimit:
    assert row.id is not None
    return BudgetLimit(
        id=row.id,
        scope=row.scope,
        monthly_usd=Decimal(row.monthly_usd),
        hard_stop=bool(row.hard_stop),
        project_id=row.project_id,
        user_id=row.user_id,
    )


def load_budgets(org_id: int | None = None) -> dict[int, OrgBudgets]:
    """Read ``budgets`` into :class:`OrgBudgets` per org.

    Parameters
    ----------
    org_id : int, optional
        Read only this org (default: every org with a budget).

    Returns
    -------
    dict[int, OrgBudgets]
        Orgs without a budget are absent.
    """
    statement = select(Budget, Organization.slug).join(
        Organization, col(Organization.id) == col(Budget.org_id)
    )
    if org_id is not None:
        statement = statement.where(col(Budget.org_id) == org_id)
    parts: dict[int, dict] = {}
    with get_session() as db:
        for row, slug in db.exec(statement).all():
            org = parts.setdefault(
                row.org_id,
                {"org_slug": slug, "members": {}, "projects": {}},
            )
            limit = _limit(row)
            if row.scope == "org":
                org["org"] = limit
            elif row.scope == "member_default":
                org["member_default"] = limit
            elif row.scope == "member" and row.user_id is not None:
                org["members"][row.user_id] = limit
            elif row.scope == "project" and row.project_id is not None:
                org["projects"][row.project_id] = limit
    return {key: OrgBudgets(**value) for key, value in parts.items()}


def load_fired(month: str) -> list[tuple[int, int | None, str, int]]:
    """This month's ``budget_alerts`` as ``(budget_id, user_id, month, threshold)``.

    Parameters
    ----------
    month : str
        ``"YYYY-MM"``.

    Returns
    -------
    list of tuple
        One per recorded crossing.
    """
    with get_session() as db:
        rows = db.exec(
            select(
                BudgetAlert.budget_id,
                BudgetAlert.user_id,
                BudgetAlert.month,
                BudgetAlert.threshold,
            ).where(col(BudgetAlert.month) == month)
        ).all()
    return [(r[0], r[1], r[2], int(r[3])) for r in rows]


def seed_budgets(book: BudgetBook) -> None:
    """Load every org's budgets and this month's fired alerts into ``book``.

    Parameters
    ----------
    book : BudgetBook
        The portal's budget book.

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        When the tables cannot be read; the caller leaves the portal
        degraded (fail closed).
    """
    book.replace_all(load_budgets())
    book.seed_fired(load_fired(current_month()))


def reload_org_budgets(book: BudgetBook, org_id: int) -> None:
    """Re-read one org's budgets into ``book`` (after anything changed them).

    Parameters
    ----------
    book : BudgetBook
        The portal's budget book.
    org_id : int
        The organization (a deleted one ends up with no budgets).
    """
    book.set_org(org_id, load_budgets(org_id).get(org_id))


# ---------------------------------------------------------------------------
# Recording an alert (the usage writer's thread)
# ---------------------------------------------------------------------------


def record_alert(alert: _Alert) -> None:
    """Insert one ``budget_alerts`` row and, when it is new, audit the crossing.

    Runs on the usage writer's thread (:meth:`BudgetBook._fire` defers it
    there). ``ON CONFLICT DO NOTHING``: a crossing already recorded (by a
    previous process) is neither stored nor audited again. A budget deleted
    meanwhile records nothing. Never raises.

    Parameters
    ----------
    alert : _Alert
        The crossing.
    """
    budget = alert.budget
    try:
        with get_session() as db:
            inserted = db.execute(
                pg_insert(BudgetAlert)
                .values(
                    budget_id=budget.id,
                    user_id=alert.user_id,
                    month=alert.month,
                    threshold=alert.threshold,
                    crossed_at=alert.crossed_at,
                    spent_usd=alert.spent,
                )
                .on_conflict_do_nothing(constraint="uq_budget_alerts_once")
            ).rowcount
            if not inserted:
                return
            member = alert.user_id if alert.user_id is not None else budget.user_id
            target = (
                None
                if member is None
                else db.exec(select(User.uid).where(User.id == member)).first()
            )
            project = (
                None
                if budget.project_id is None
                else db.exec(
                    select(Project.slug).where(Project.id == budget.project_id)
                ).first()
            )
    except SQLAlchemyError:
        _log.warning(
            "budgets: could not record the %d%% alert of budget %s",
            alert.threshold,
            budget.id,
            exc_info=True,
        )
        return
    fields = {
        "org_id": alert.org_id,
        "org": alert.org_slug,
        "target": target,
        "scope": budget_scope(budget.scope),
        "project": project,
        "threshold": alert.threshold,
        "spent_usd": _usd(alert.spent),
        "budget_usd": _usd(budget.monthly_usd),
        "month": alert.month,
    }
    if project is None:
        del fields["project"]  # only a project budget names one
    audit("budget_threshold_crossed", {}, **fields)
    if alert.threshold == 100 and budget.hard_stop:
        audit("budget_hard_stop_engaged", {}, **fields)
