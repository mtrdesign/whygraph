"""The admin session (plan 59 section 4.1): kind, lifetime, pairing, cookie.

Step 1 covers the session layer and the migration: the admin cookie's
attributes (asserted on the ``Set-Cookie`` header - httpx's cookie jar is
liberal about domains, so what the jar sends proves nothing about
host-only), the per-kind lifetimes (8 hours absolute and no idle limit for
the admin kind, D50), pairing (D51), D4's refusal of a password account's
everyday session and connection token, and the migration in both
directions. Step 2 adds the request resolution and guard cases.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`) imported from test_portal_app

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from starlette.responses import Response

from test_portal_app import (  # noqa: F401 -- fixtures
    PROD_BASE,
    admin_signed_in,
    env,
    prod_portal,
    production_env,
)
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal import sessions
from whygraph.portal.hosts import BaseUrl
from whygraph.portal.models import ConnectionToken, Project, User, UserSession
from whygraph.portal.orgs import add_member, create_org

DEV = BaseUrl.parse(PROD_BASE)
HTTPS = BaseUrl.parse("https://whygraph.example.com")
UPDATE_CHECK = "496953df26b4"
ADMIN_SESSION = "df854cdd4f03"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _user(
    email: str, *, admin: bool = False, password: bool = False, github_id: int = 0
) -> int:
    with portal_db.get_session() as session:
        user = User(display_name=email.split("@")[0].title(), email=email)
        user.is_instance_admin = admin
        if password:
            user.password_hash = "argon2-placeholder"
        if github_id:
            user.github_id = github_id
            user.github_login = email.split("@")[0]
        session.add(user)
        session.flush()
        assert user.id is not None
        return user.id


def _start(user_id: int, kind: str = "user", paired: int | None = None) -> str:
    with portal_db.get_session() as session:
        return sessions.create_session(
            session, user_id, None, kind=kind, paired_session_id=paired
        )


def _row(token: str) -> UserSession | None:
    with portal_db.get_session() as session:
        row = session.exec(
            select(UserSession).where(
                UserSession.token_hash == sessions.hash_token(token)
            )
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def _set(token: str, **values: object) -> None:
    with portal_db.get_session() as session:
        row = session.exec(
            select(UserSession).where(
                UserSession.token_hash == sessions.hash_token(token)
            )
        ).one()
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)


def _cookie_parts(header: str) -> dict[str, str]:
    return {p.strip().split("=")[0].lower(): p.strip() for p in header.split(";")}


# ---------------------------------------------------------------------------
# The admin cookie
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "name", "secure"),
    [(DEV, "whygraph_admin", False), (HTTPS, "__Host-whygraph_admin", True)],
)
def test_the_admin_cookie_attributes(base: BaseUrl, name: str, secure: bool) -> None:
    assert sessions.admin_cookie_name(base) == name
    response = Response()
    sessions.set_admin_cookie(response, "tok", base)
    (header,) = response.headers.getlist("set-cookie")
    parts = _cookie_parts(header)
    assert header.startswith(f"{name}=tok;")
    assert "domain" not in parts  # host-only: never sent to an org host
    assert parts["path"] == "Path=/"
    assert parts["max-age"] == f"Max-Age={8 * 3600}"
    assert "httponly" in parts
    assert parts["samesite"].lower() == "samesite=strict"
    assert ("secure" in parts) is secure


def test_clearing_the_admin_cookie_on_https_is_one_host_only_header() -> None:
    response = Response()
    sessions.clear_admin_cookie(response, HTTPS)
    (header,) = response.headers.getlist("set-cookie")
    parts = _cookie_parts(header)
    assert header.startswith("__Host-whygraph_admin=")
    assert "Max-Age=0" in header
    assert "domain" not in parts  # a __Host- cookie with a Domain is rejected
    assert "secure" in parts and parts["path"] == "Path=/"
    assert [header.encode()] == [v for _, v in sessions.admin_clearing_headers(HTTPS)]


def test_clearing_the_admin_cookie_on_http_covers_both_forms() -> None:
    response = Response()
    sessions.clear_admin_cookie(response, DEV)
    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert all(h.startswith("whygraph_admin=") and "Max-Age=0" in h for h in headers)
    assert sum(f"Domain={DEV.host}" in h for h in headers) == 1
    assert not any("secure" in _cookie_parts(h) for h in headers)
    assert [h.encode() for h in headers] == [
        v for _, v in sessions.admin_clearing_headers(DEV)
    ]


def test_session_cookies_reads_either_cookie_by_name() -> None:
    headers = [
        "whygraph_session=u; whygraph_admin=a1",
        'whygraph_admin="a2"; __Host-whygraph_admin=h',
    ]
    assert sessions.session_cookies(headers) == ["u"]
    assert sessions.session_cookies(headers, name="whygraph_admin") == ["a1", "a2"]
    assert sessions.session_cookies(
        headers, name=sessions.admin_cookie_name(HTTPS)
    ) == ["h"]


# ---------------------------------------------------------------------------
# Kinds and lifetimes
# ---------------------------------------------------------------------------


def test_an_admin_session_is_its_own_kind(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    admin = _start(ada, "admin")
    user = _start(ada)
    admin_row, user_row = _row(admin), _row(user)
    assert admin_row is not None and user_row is not None
    assert admin_row.kind == "admin" and user_row.kind == "user"
    created = datetime.fromisoformat(admin_row.created_at)
    assert datetime.fromisoformat(admin_row.expires_at) - created == timedelta(hours=8)
    created = datetime.fromisoformat(user_row.created_at)
    assert datetime.fromisoformat(user_row.expires_at) - created == timedelta(days=30)
    # Each token is unknown to the other kind's lookup.
    assert sessions.lookup(admin) is None
    assert sessions.lookup(user, kind="admin") is None
    found = sessions.lookup(admin, kind="admin")
    assert found is not None and found.user_id == ada and found.kind == "admin"
    assert found.created_at == admin_row.created_at
    assert found.expires_at == admin_row.expires_at
    everyday = sessions.lookup(user)
    assert everyday is not None and everyday.kind == "user"
    assert everyday.is_instance_admin is True  # shown, but opens no admin route
    with pytest.raises(ValueError, match="session kind"):
        sessions.lookup(admin, kind="root")
    with pytest.raises(ValueError, match="session kind"):
        _start(ada, "root")


def test_the_database_refuses_an_unknown_kind(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    token = _start(ada, "admin")
    with pytest.raises(IntegrityError, match="ck_sessions_kind"):
        _set(token, kind="root")


def test_an_admin_session_expires_after_eight_hours(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    expired, live = _start(ada, "admin"), _start(ada, "admin")
    now = datetime.now(timezone.utc)
    _set(
        expired,
        created_at=_iso(now - timedelta(hours=8, seconds=1)),
        expires_at=_iso(now - timedelta(seconds=1)),
    )
    assert sessions.lookup(expired, kind="admin") is None
    assert sessions.lookup(live, kind="admin") is not None
    _start(ada)  # any sign-in sweeps an expired admin row
    assert _row(expired) is None and _row(live) is not None


def test_an_admin_session_has_no_idle_limit(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    admin, user = _start(ada, "admin"), _start(ada)
    idle = _iso(datetime.now(timezone.utc) - sessions.IDLE - timedelta(seconds=1))
    for token in (admin, user):
        _set(token, last_seen_at=idle)  # expires_at is still ahead
    assert sessions.lookup(admin, kind="admin") is not None
    assert sessions.lookup(user) is None
    _start(ada)  # the sweep
    assert _row(admin) is not None
    assert _row(user) is None


def test_demoting_or_disabling_the_admin_ends_the_admin_session(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    admin, user = _start(ada, "admin"), _start(ada)
    with portal_db.get_session() as session:
        session.get(User, ada).is_instance_admin = False
    assert sessions.lookup(admin, kind="admin") is None
    assert sessions.lookup(user) is not None  # the everyday session stays
    with portal_db.get_session() as session:
        row = session.get(User, ada)
        row.is_instance_admin = True
        row.disabled_at = _iso(datetime.now(timezone.utc))
    assert sessions.lookup(admin, kind="admin") is None


def test_a_password_accounts_user_session_is_refused(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, password=True)
    user, admin = _start(ada), _start(ada, "admin")
    assert sessions.lookup(user) is None
    found = sessions.lookup(admin, kind="admin")
    assert found is not None and found.has_password is True


def test_revoke_user_ends_both_kinds_and_revoke_user_kind_one(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    bob = _user("bob@example.com", admin=True, github_id=2)
    admin, user = _start(ada, "admin"), _start(ada)
    bob_admin = _start(bob, "admin")
    sessions.revoke_user_kind(ada, "admin")
    assert _row(admin) is None and _row(user) is not None
    assert _row(bob_admin) is not None
    admin = _start(ada, "admin")
    with portal_db.get_session() as session:
        sessions.revoke_user(ada, db=session)
    assert _row(admin) is None and _row(user) is None
    assert _row(bob_admin) is not None


def test_rotate_keeps_the_kind_and_unpairs(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, password=True)
    bea = _user("bea@example.com", admin=True, github_id=2)
    everyday = _start(bea)
    paired = _start(bea, "admin", paired=_row(everyday).id)
    with portal_db.get_session() as session:
        rotated = sessions.rotate(session, _row(paired).id, bea, None)
    assert _row(paired) is None
    row = _row(rotated)
    assert row.kind == "admin" and row.paired_session_id is None
    admin = _start(ada, "admin")
    with portal_db.get_session() as session:
        rotated = sessions.rotate(session, _row(admin).id, ada, None)
    assert sessions.lookup(rotated, kind="admin") is not None
    assert sessions.lookup(admin, kind="admin") is None


# ---------------------------------------------------------------------------
# Pairing (D51)
# ---------------------------------------------------------------------------


def test_end_paired_admin_ends_only_the_admin_sessions_paired_with_it(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    laptop, phone = _start(ada), _start(ada)
    on_laptop = _start(ada, "admin", paired=_row(laptop).id)
    on_phone = _start(ada, "admin", paired=_row(phone).id)
    unpaired = _start(ada, "admin")
    with portal_db.get_session() as session:
        assert sessions.end_paired_admin(session, _row(laptop).id) == 1
        assert sessions.end_paired_admin(session, _row(laptop).id) == 0
    assert _row(on_laptop) is None
    assert _row(on_phone) is not None and _row(unpaired) is not None
    assert _row(laptop) is not None  # the everyday row is the caller's to end


def test_repair_admin_moves_the_pairing(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    old, other = _start(ada), _start(ada)
    admin = _start(ada, "admin", paired=_row(old).id)
    elsewhere = _start(ada, "admin", paired=_row(other).id)
    new = _start(ada)
    with portal_db.get_session() as session:
        sessions.repair_admin(session, _row(old).id, _row(new).id)
        sessions.revoke(_row(old).id, db=session)
    assert _row(admin).paired_session_id == _row(new).id
    assert _row(elsewhere).paired_session_id == _row(other).id


def test_deleting_the_everyday_row_unpairs_and_never_ends_the_admin_session(
    production_env: SimpleNamespace,
) -> None:
    """``fk_sessions_paired`` is ``ON DELETE SET NULL``: a sweep or revoke
    of the everyday row leaves the admin session alive, unpaired."""
    portal_db.ensure_initialized()
    ada = _user("ada@example.com", admin=True, github_id=1)
    revoked, swept = _start(ada), _start(ada)
    by_revoke = _start(ada, "admin", paired=_row(revoked).id)
    by_sweep = _start(ada, "admin", paired=_row(swept).id)
    sessions.revoke(_row(revoked).id)
    _set(swept, expires_at=_iso(datetime.now(timezone.utc) - timedelta(seconds=1)))
    _start(ada)  # the sweep deletes the expired everyday row
    assert _row(swept) is None
    for token in (by_revoke, by_sweep):
        row = _row(token)
        assert row is not None and row.paired_session_id is None
        assert sessions.lookup(token, kind="admin") is not None


def test_admin_signed_in_sets_the_admin_cookie(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        ada = _user("ada@example.com", admin=True, github_id=1)
        token = admin_signed_in(client, ada)
        assert client.cookies.get("whygraph_admin") == token
        assert client.cookies.get("whygraph_session") is None
        found = sessions.lookup(token, kind="admin")
        assert found is not None and found.user_id == ada
        assert _row(token).paired_session_id is None


# ---------------------------------------------------------------------------
# Connection tokens of password accounts (D4)
# ---------------------------------------------------------------------------


def _token_world() -> SimpleNamespace:
    """Org ``acme`` with project ``api``; a password owner and a GitHub owner."""
    pat = _user("pat@example.com", admin=True, password=True)
    gil = _user("gil@example.com", github_id=7)
    with portal_db.get_session() as session:
        org = create_org(session, slug="acme", name="Acme")
        for uid in (pat, gil):
            add_member(session, org_id=org.id, user_id=uid, role="owner")
        project = Project(
            org_id=org.id, slug="api", name="API", source="local", root="/r/api"
        )
        session.add(project)
        session.flush()
        tokens = {
            name: connections.issue(
                session,
                user=session.get(User, uid),
                project=project,
                client_name="laptop",
            )
            for name, uid in (("pat", pat), ("gil", gil))
        }
    return SimpleNamespace(tokens=tokens)


def test_connections_lookup_refuses_a_password_accounts_token(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    world = _token_world()
    with portal_db.get_session() as session:
        refused = connections.lookup(session, world.tokens["pat"])
        live = connections.lookup(session, world.tokens["gil"])
    assert refused == connections.Refusal("token_revoked", "admin_revoked")
    assert isinstance(live, connections.TokenPrincipal)
    with portal_db.get_session() as session:  # the refusal writes nothing
        row = session.exec(
            select(ConnectionToken).where(
                ConnectionToken.token_hash
                == connections.hash_token(world.tokens["pat"])
            )
        ).one()
        assert row.revoked_at is None and row.revoked_reason is None


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def _seed_pre_admin_session(conn) -> dict[str, int]:  # noqa: ANN001
    """Rows as the update-check revision holds them: no ``sessions.kind``."""
    org = conn.execute(
        text(
            "INSERT INTO organizations (uid, slug, name, created_at, "
            "default_project_role) VALUES ('o-acme', 'acme', 'Acme', 'now', "
            "'contributor') RETURNING id"
        )
    ).scalar_one()
    project = conn.execute(
        text(
            "INSERT INTO projects (org_id, slug, name, source, root, created_at, "
            "restricted) VALUES (:org, 'api', 'API', 'local', '/r/api', 'now', "
            "false) RETURNING id"
        ),
        {"org": org},
    ).scalar_one()
    ids = {"org": org, "project": project}
    for name, password in (("pat", "'hash'"), ("gil", "NULL")):
        ids[name] = conn.execute(
            text(
                "INSERT INTO users (uid, display_name, email, password_hash, "
                f"is_instance_admin, created_at) VALUES ('u-{name}', '{name}', "
                f"'{name}@example.com', {password}, true, 'now') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, created_at, "
                "last_seen_at, expires_at) VALUES (:hash, :user, 'now', 'now', "
                "'9999')"
            ),
            {"hash": f"session-{name}", "user": ids[name]},
        )
        for suffix, revoked in (("live", None), ("old", "member_left")):
            conn.execute(
                text(
                    "INSERT INTO connection_tokens (uid, user_id, org_id, "
                    "project_id, token_hash, client_name, created_at, "
                    "revoked_at, revoked_reason) VALUES (:uid, :user, :org, "
                    ":project, :hash, 'laptop', 'now', :at, :reason)"
                ),
                {
                    "uid": f"t-{name}-{suffix}",
                    "user": ids[name],
                    "org": org,
                    "project": project,
                    "hash": f"token-{name}-{suffix}",
                    "at": None if revoked is None else "then",
                    "reason": revoked,
                },
            )
    return ids


def test_the_migration_ends_password_accounts_sessions_and_tokens(
    empty_portal_database: str,
) -> None:
    command.upgrade(portal_db.alembic_config(), UPDATE_CHECK)
    with portal_db.get_engine().begin() as conn:
        ids = _seed_pre_admin_session(conn)
    portal_db._reset_engine()
    started = _iso(datetime.now(timezone.utc))
    command.upgrade(portal_db.alembic_config(), "head")
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == ADMIN_SESSION
        )
        left = conn.execute(
            text("SELECT user_id, kind, paired_session_id FROM sessions")
        ).all()
        assert left == [(ids["gil"], "user", None)]  # backfilled as user
        tokens = {
            uid: (at, reason)
            for uid, at, reason in conn.execute(
                text("SELECT uid, revoked_at, revoked_reason FROM connection_tokens")
            )
        }
    at, reason = tokens["t-pat-live"]
    assert reason == "admin_revoked" and at >= started  # the models._now() format
    assert datetime.fromisoformat(at).tzinfo is not None
    assert tokens["t-pat-old"] == ("then", "member_left")  # already revoked: kept
    assert tokens["t-gil-live"] == (None, None)
    assert tokens["t-gil-old"] == ("then", "member_left")


def test_the_downgrade_deletes_admin_rows(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_engine().begin() as conn:
        user = conn.execute(
            text(
                "INSERT INTO users (uid, display_name, is_instance_admin, "
                "github_id, created_at) VALUES ('u-ada', 'Ada', true, 1, 'now') "
                "RETURNING id"
            )
        ).scalar_one()
        everyday = conn.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, created_at, "
                "last_seen_at, expires_at, kind) VALUES ('u', :user, 'now', "
                "'now', '9999', 'user') RETURNING id"
            ),
            {"user": user},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, created_at, "
                "last_seen_at, expires_at, kind, paired_session_id) VALUES "
                "('a', :user, 'now', 'now', '9999', 'admin', :paired)"
            ),
            {"user": user, "paired": everyday},
        )
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), UPDATE_CHECK)
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT token_hash FROM sessions")
        ).scalars().all() == ["u"]
        columns = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'sessions'"
                )
            )
        }
        assert not columns & {"kind", "paired_session_id"}
        indexes = set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'sessions'")
            ).scalars()
        )
        assert "ix_sessions_paired_session_id" not in indexes
    command.upgrade(portal_db.alembic_config(), "head")
