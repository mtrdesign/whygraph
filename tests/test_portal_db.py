"""Tests for the portal DB, models, migrations, secrets and context builder.

Covers plan step 3: a fresh migrate, the separate ``MetaData`` (both
directions), the partial-unique / cascade constraints, the Fernet key file
and keyring, the secret store's API shape, and building a
``ProjectContext`` under the config policy (rules 2 and 3 of section 4.2.1).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Iterator

import anyio
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from whygraph.core.config import Config
from whygraph.core.context import ProjectContext
from whygraph.portal import db as portal_db
from whygraph.portal import secrets as pw_secrets
from whygraph.portal.config_layers import (
    ConfigPolicyError,
    find_secret_paths,
    load_layer,
    save_layer,
)
from whygraph.portal.context import (
    ContextCache,
    ProjectNotFound,
    build_project_context,
)
from whygraph.portal.models import (
    PortalBase,
    Project,
    ProjectAgent,
    ProjectConfig,
    ScanRun,
    Secret,
    Setting,
    User,
)
from whygraph.portal.projects import (
    is_valid_slug,
    slugify,
    unique_slug,
    validate_slug,
)

PORTAL_TABLES = {
    "settings",
    "users",
    "projects",
    "project_agents",
    "project_config",
    "secrets",
    "scan_runs",
}


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated, migrated portal data dir."""
    path = tmp_path / "data"
    monkeypatch.setenv("WHYGRAPH_DATA", str(path))
    portal_db._reset_engine()
    portal_db.ensure_initialized()
    try:
        yield path
    finally:
        portal_db._reset_engine()


def _project(session, slug: str = "demo", root: str | None = None, **kw) -> Project:
    project = Project(
        slug=slug,
        name=slug,
        source=kw.pop("source", "local"),
        root=root or f"/repos/{slug}",
        **kw,
    )
    session.add(project)
    session.flush()
    return project


# ---------------------------------------------------------------------------
# Migrations and metadata
# ---------------------------------------------------------------------------


def test_fresh_migrate_creates_portal_tables(data: Path) -> None:
    conn = sqlite3.connect(data / "portal.db")
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert names == PORTAL_TABLES | {"alembic_version"}


def test_data_dir_is_private(data: Path) -> None:
    assert stat.S_IMODE(data.stat().st_mode) & 0o077 == 0


def test_migrate_is_idempotent(data: Path) -> None:
    portal_db.ensure_initialized()
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        assert s.exec(select(Setting)).all() == []


def test_metadata_is_separate_in_both_directions() -> None:
    from whygraph.db import models as _project_models  # noqa: F401

    portal = set(PortalBase.metadata.tables)
    project = set(SQLModel.metadata.tables)
    assert portal == PORTAL_TABLES
    assert not portal & project
    assert "chat_session" not in portal
    assert "projects" not in project


def _diff_table_names(metadata, *, include_object=None) -> set[str]:
    """Table names an autogenerate against an *empty* DB would create."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ctx = MigrationContext.configure(
            conn, opts={"include_object": include_object} if include_object else {}
        )
        diff = compare_metadata(ctx, metadata)
    return {op[1].name for op in diff if op[0] == "add_table"}


def test_autogenerate_never_crosses_the_chains() -> None:
    from whygraph.db import models as _project_models  # noqa: F401

    def project_include(obj, name, type_, reflected, compare_to):  # noqa: ANN001
        # the same filter whygraph/db/migrations/env.py applies
        return not (type_ == "table" and name not in SQLModel.metadata.tables)

    project_tables = _diff_table_names(
        SQLModel.metadata, include_object=project_include
    )
    portal_tables = _diff_table_names(PortalBase.metadata)
    assert project_tables and not project_tables & PORTAL_TABLES
    assert portal_tables == PORTAL_TABLES


def test_migration_matches_models(data: Path) -> None:
    """No drift between the migration and the models (expression indexes aside)."""
    with portal_db.get_engine().connect() as conn, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, PortalBase.metadata)
    assert diff == []


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_saving_global_config_twice_leaves_one_row(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/a"}})
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/b"}})
    with portal_db.get_session() as s:
        rows = s.exec(
            select(ProjectConfig).where(ProjectConfig.project_id.is_(None))
        ).all()
        assert len(rows) == 1
        assert rows[0].config == {"llm": {"model": "anthropic/b"}}
        assert load_layer(s, None) == {"llm": {"model": "anthropic/b"}}


def test_saving_project_config_twice_leaves_one_row(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": 2}})
        save_layer(s, p.id, {"analyze": {"max_workers": 3}})
    with portal_db.get_session() as s:
        assert len(s.exec(select(ProjectConfig)).all()) == 1


def test_duplicate_global_rows_are_rejected_by_the_index(data: Path) -> None:
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectConfig(project_id=None, config={}))
            s.flush()
            s.add(ProjectConfig(project_id=None, config={"a": 1}))
            s.flush()


def test_saving_a_global_secret_twice_leaves_one_row(data: Path) -> None:
    for value in ("sk-first-1111", "sk-second-2222"):
        with portal_db.get_session() as s:
            pw_secrets.put_secret(
                s, kind="llm_api_key", provider="anthropic", value=value
            )
    with portal_db.get_session() as s:
        assert len(s.exec(select(Secret)).all()) == 1
        assert (
            pw_secrets.read_secret(s, kind="llm_api_key", provider="anthropic")
            == "sk-second-2222"
        )


def test_github_token_uniqueness_holds_despite_null_provider(data: Path) -> None:
    """A NULL provider must not let duplicate GitHub tokens slip past UNIQUE."""
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(Secret(kind="github_token", ciphertext="x", hint="x"))
                s.flush()


def test_project_secret_uniqueness_holds_despite_null_provider(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        pid = p.id
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(
                    Secret(
                        project_id=pid, kind="github_token", ciphertext="x", hint="x"
                    )
                )
                s.flush()


def test_secret_kind_and_provider_are_checked(data: Path) -> None:
    for bad in (
        Secret(kind="nonsense", ciphertext="x", hint="x"),
        Secret(kind="llm_api_key", provider=None, ciphertext="x", hint="x"),
        Secret(kind="github_token", provider="anthropic", ciphertext="x", hint="x"),
    ):
        with pytest.raises(IntegrityError):
            with portal_db.get_session() as s:
                s.add(bad)
                s.flush()


def test_deleting_a_project_cascades(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        other = _project(s, "other")
        pid, oid = p.id, other.id
        save_layer(s, pid, {"analyze": {"max_workers": 2}})
        save_layer(s, oid, {"analyze": {"max_workers": 4}})
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-abcd", project_id=pid
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_xxxx", project_id=oid)
        s.add(ProjectAgent(project_id=pid, agent="claude"))
        s.add(ScanRun(project_id=pid, trigger="manual"))
        s.add(ScanRun(project_id=oid, trigger="hook"))
    with portal_db.get_session() as s:
        s.delete(s.get(Project, pid))
    with portal_db.get_session() as s:
        assert [r.project_id for r in s.exec(select(ProjectConfig)).all()] == [oid]
        assert [r.project_id for r in s.exec(select(Secret)).all()] == [oid]
        assert s.exec(select(ProjectAgent)).all() == []
        assert [r.project_id for r in s.exec(select(ScanRun)).all()] == [oid]


def test_deleting_a_user_nulls_attribution(data: Path) -> None:
    with portal_db.get_session() as s:
        u = User(display_name="Ada")
        s.add(u)
        s.flush()
        p = _project(s, created_by=u.id)
        s.add(ScanRun(project_id=p.id, trigger="manual", requested_by=u.id))
        uid = u.id
    with portal_db.get_session() as s:
        s.delete(s.get(User, uid))
    with portal_db.get_session() as s:
        assert s.exec(select(Project)).one().created_by is None
        assert s.exec(select(ScanRun)).one().requested_by is None


def test_user_has_a_stable_uuid(data: Path) -> None:
    with portal_db.get_session() as s:
        a, b = User(display_name="a"), User(display_name="b")
        s.add(a)
        s.add(b)
        s.flush()
        assert len(a.uid) == 36 and a.uid != b.uid
        assert a.role == "owner" and a.username is None and a.password_hash is None


def test_project_agents_rejects_unknown_agent_and_duplicates(data: Path) -> None:
    with portal_db.get_session() as s:
        pid = _project(s).id
        s.add(ProjectAgent(project_id=pid, agent="claude"))
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectAgent(project_id=pid, agent="emacs"))
            s.flush()
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectAgent(project_id=pid, agent="claude"))
            s.flush()
    with portal_db.get_session() as s:
        for agent in ("cursor", "vscode", "codex"):
            s.add(ProjectAgent(project_id=pid, agent=agent))
    with portal_db.get_session() as s:
        assert len(s.exec(select(ProjectAgent)).all()) == 4


def test_settings_is_a_singleton(data: Path) -> None:
    with portal_db.get_session() as s:
        s.add(Setting(mode="local"))
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(Setting(id=2, mode="local"))
            s.flush()
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.execute(text("UPDATE settings SET mode = 'bogus'"))


def test_project_slug_and_root_are_unique(data: Path) -> None:
    with portal_db.get_session() as s:
        _project(s, "a", root="/r/a")
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            _project(s, "a", root="/r/other")
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            _project(s, "b", root="/r/a")


def test_m2_columns_exist(data: Path) -> None:
    insp = inspect(portal_db.get_engine())
    cols = {
        t: {c["name"] for c in insp.get_columns(t)}
        for t in ("users", "projects", "scan_runs")
    }
    assert {"uid", "username", "password_hash", "role"} <= cols["users"]
    assert {"created_by", "last_scanned_head", "initialized_at"} <= cols["projects"]
    assert {"requested_by", "analyze", "trigger", "kind"} <= cols["scan_runs"]


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["a", "whygraph", "my-repo-2", "0abc", "a" * 63])
def test_valid_slugs(slug: str) -> None:
    assert is_valid_slug(slug) and validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug", ["", "-a", "A", "a b", "a_b", "a/b", "a.b", "a" * 64, "a\n", "../x", "é"]
)
def test_invalid_slugs(slug: str) -> None:
    assert not is_valid_slug(slug)
    with pytest.raises(ValueError):
        validate_slug(slug)


def test_slugify() -> None:
    assert slugify("My Repo_v2") == "my-repo-v2"
    assert slugify("--Hello!!World--") == "hello-world"
    assert slugify("???") == "project"
    assert is_valid_slug(slugify("x" * 200))


def test_unique_slug_suffixes_on_collision(data: Path) -> None:
    with portal_db.get_session() as s:
        assert unique_slug(s, "Whygraph") == "whygraph"
        _project(s, "whygraph")
        assert unique_slug(s, "Whygraph") == "whygraph-2"
        _project(s, "whygraph-2")
        assert unique_slug(s, "whygraph") == "whygraph-3"
        long = "x" * 63
        _project(s, long)
        assert is_valid_slug(unique_slug(s, long))


def test_slug_is_validated_on_insert_and_immutable(data: Path) -> None:
    with pytest.raises(ValueError):
        with portal_db.get_session() as s:
            _project(s, "Bad Slug")
    with portal_db.get_session() as s:
        _project(s, "keep")
    with portal_db.get_session() as s:
        project = s.exec(select(Project)).one()
        project.name = "Renamed"  # only the name is renamable
    with pytest.raises(ValueError, match="immutable"):
        with portal_db.get_session() as s:
            s.exec(select(Project)).one().slug = "changed"
            s.flush()
    with portal_db.get_session() as s:
        project = s.exec(select(Project)).one()
        assert (project.slug, project.name) == ("keep", "Renamed")


# ---------------------------------------------------------------------------
# Key file, keyring, secret store
# ---------------------------------------------------------------------------


def test_key_file_is_private_and_stable(data: Path) -> None:
    key = pw_secrets.ensure_key()
    path = data / "secret.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pw_secrets.ensure_key() == key
    assert list(data.glob("secret.key.*.tmp")) == []


def test_keyring_round_trips_and_ciphertext_differs_per_write(data: Path) -> None:
    a, b = pw_secrets.encrypt("sk-secret"), pw_secrets.encrypt("sk-secret")
    assert a != b and "sk-secret" not in a
    assert pw_secrets.decrypt(a) == pw_secrets.decrypt(b) == "sk-secret"
    assert len(pw_secrets.load_keyring()._fernets) == 1  # M1: one key, MultiFernet


def test_secrets_round_trip_and_ciphertext_differs_per_write(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2"
        )
        first = s.exec(select(Secret)).one().ciphertext
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2"
        )
        second = s.exec(select(Secret)).one().ciphertext
        assert (
            pw_secrets.read_secret(s, kind="llm_api_key", provider="anthropic")
            == "sk-ant-a1b2"
        )
    assert first != second
    raw = sqlite3.connect(data / "portal.db")
    try:
        stored = raw.execute("SELECT ciphertext, hint FROM secrets").fetchone()
    finally:
        raw.close()
    assert "sk-ant" not in stored[0] and "sk-ant" not in stored[1]


def test_secret_status_exposes_only_set_and_hint(data: Path) -> None:
    with portal_db.get_session() as s:
        assert pw_secrets.secret_status(s, kind="github_token") == {
            "set": False,
            "hint": None,
        }
        pw_secrets.put_secret(s, kind="github_token", value="ghp_abcdefa1b2")
        status = pw_secrets.secret_status(s, kind="github_token")
    assert status == {"set": True, "hint": "…a1b2"}
    assert "ghp_" not in repr(status)


def test_unreadable_secret_is_flagged_not_fatal(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="github_token", value="ghp_abcdefa1b2")
        s.exec(select(Secret)).one().ciphertext = "not-a-fernet-token"
    with portal_db.get_session() as s:
        status = pw_secrets.secret_status(s, kind="github_token")
    assert status == {"set": True, "hint": "…a1b2", "unreadable": True}


def test_secret_store_validates_scope(data: Path) -> None:
    with portal_db.get_session() as s:
        for kw in (
            dict(kind="llm_api_key", provider="ollama", value="x"),
            dict(kind="llm_api_key", provider=None, value="x"),
            dict(kind="github_token", provider="anthropic", value="x"),
            dict(kind="weird", value="x"),
            dict(kind="github_token", value="   "),
        ):
            with pytest.raises(ValueError):
                pw_secrets.put_secret(s, **kw)


def test_delete_secret(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="github_token", value="ghp_1234")
        assert pw_secrets.delete_secret(s, kind="github_token") is True
        assert pw_secrets.delete_secret(s, kind="github_token") is False


_RACER = """
import os, sys, time
os.environ["WHYGRAPH_DATA"] = sys.argv[1]
go = sys.argv[2]
while not os.path.exists(go):
    time.sleep(0.001)
from whygraph.portal import secrets
print(secrets.ensure_key().decode())
"""


def test_two_processes_racing_end_with_one_complete_key(tmp_path: Path) -> None:
    data = tmp_path / "race-data"
    data.mkdir()
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, str(data), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    time.sleep(1.0)  # let every child import whygraph and reach the barrier
    go.touch()
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    keys = {out.strip() for out, _ in outs}
    assert len(keys) == 1
    on_disk = (data / "secret.key").read_bytes().strip().decode()
    assert keys == {on_disk} and len(on_disk) == 44
    assert list(data.glob("secret.key.*.tmp")) == []


def test_key_race_loser_reads_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    winner = pw_secrets.Fernet.generate_key()
    real_link = os.link

    def racing_link(src, dst, *a, **kw):  # noqa: ANN001
        Path(dst).write_bytes(winner)  # the other process publishes first
        return real_link(src, dst, *a, **kw)  # -> FileExistsError

    monkeypatch.setattr(os, "link", racing_link)
    assert pw_secrets.ensure_key() == winner
    assert Path(tmp_path / "d" / "secret.key").read_bytes() == winner
    assert list((tmp_path / "d").glob("secret.key.*.tmp")) == []


def test_stale_tmp_from_a_reused_pid_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / f"secret.key.{os.getpid()}.tmp").write_text("stale")
    key = pw_secrets.ensure_key()
    assert (tmp_path / "d" / "secret.key").read_bytes() == key


def test_corrupt_key_file_is_an_error_not_a_silent_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "secret.key").write_text("garbage")
    with pytest.raises(ValueError):
        pw_secrets.ensure_key()


# ---------------------------------------------------------------------------
# Config policy
# ---------------------------------------------------------------------------


def test_secrets_in_config_dict_are_rejected(data: Path) -> None:
    bad = {"llm": {"anthropic": {"api_key": "sk-x"}}, "scan": {"token": "ghp_x"}}
    assert find_secret_paths(bad) == ["llm.anthropic.api_key", "scan.token"]
    with portal_db.get_session() as s:
        with pytest.raises(ConfigPolicyError) as exc:
            save_layer(s, None, bad)
        assert "sk-x" not in str(exc.value) and "ghp_x" not in str(exc.value)
        assert s.exec(select(ProjectConfig)).all() == []


def test_changing_an_endpoint_clears_that_scopes_key_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(s, None, {"llm": {"openai": {"base_url": "https://a.example/v1"}}})
        save_layer(s, pid, {})
        for scope in (None, pid):
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="openai",
                value="sk-1111",
                project_id=scope,
            )
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="anthropic",
                value="sk-2222",
                project_id=scope,
            )
        # unrelated edit: keys survive
        save_layer(
            s,
            None,
            {
                "llm": {"openai": {"base_url": "https://a.example/v1"}},
                "analyze": {"max_workers": 2},
            },
        )
        assert pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")["set"]
        # global endpoint edit clears the global openai key, and the openai
        # key of every project that inherits the endpoint (security fix 1,
        # finding 6) - never another provider's
        cleared = save_layer(
            s, None, {"llm": {"openai": {"base_url": "https://evil.example/v1"}}}
        )
        assert cleared == [(pid, "openai")]
        assert not pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")[
            "set"
        ]
        assert pw_secrets.secret_status(s, kind="llm_api_key", provider="anthropic")[
            "set"
        ]
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid
        )["set"]
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-3333", project_id=pid
        )
        # a project-scope endpoint edit clears that project's key
        save_layer(s, pid, {"llm": {"openai": {"base_url": "https://p.example/v1"}}})
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid
        )["set"]
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="anthropic", project_id=pid
        )["set"]


def test_removing_an_endpoint_also_clears_the_key(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"ollama": {"host": "http://h:1"}}})
        save_layer(s, None, {"llm": {"openai": {"base_url": "https://a/v1"}}})
        pw_secrets.put_secret(s, kind="llm_api_key", provider="openai", value="sk-1111")
        save_layer(s, None, {})
        assert not pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")[
            "set"
        ]


# ---------------------------------------------------------------------------
# Building a ProjectContext
# ---------------------------------------------------------------------------


def _build(session, project: Project) -> ProjectContext:
    return build_project_context(session, project)


def test_context_merges_layers_project_wins(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {
                "llm": {"model": "anthropic/claude-x"},
                "analyze": {"max_workers": 2},
                "scan": {"max_workers": 9},  # 1.x alias, normalized per layer
            },
        )
        p = _project(s)
        save_layer(
            s, p.id, {"analyze": {"max_workers": 6}, "scan": {"forge": "github"}}
        )
        ctx = _build(s, p)
    assert ctx.slug == "demo" and ctx.root == Path("/repos/demo")
    assert ctx.config.model_for("analyze") == ("anthropic", "claude-x")
    assert ctx.config.analyze.max_workers == 6
    assert ctx.config.scan_forge == "github"


def test_context_null_resets_a_key(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"analyze": {"max_workers": 2}})
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": None}})
        assert _build(s, p).config.analyze.max_workers == Config().analyze.max_workers


def test_db_paths_in_a_project_row_are_ignored(data: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {"whygraph_db": "/etc/global.db", "codegraph_db": str(data / "portal.db")},
        )
        p = _project(s, root=str(root))
        save_layer(
            s,
            p.id,
            {
                "whygraph_db": str(tmp_path / "other" / "whygraph.db"),
                "codegraph_db": "../x.db",
            },
        )
        ctx = _build(s, p)
    assert ctx.config.whygraph_db == root / ".whygraph" / "whygraph.db"
    assert ctx.config.codegraph_db == root / ".codegraph" / "codegraph.db"


def test_github_clone_root_resolves_under_the_data_dir(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s, "cloned", root="repos/cloned", source="github")
        ctx = _build(s, p)
    assert ctx.root == data / "repos" / "cloned"
    assert (
        ctx.config.whygraph_db
        == data / "repos" / "cloned" / ".whygraph" / "whygraph.db"
    )


def test_context_injects_secrets_in_memory_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-1111"
        )
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="claude-cli", value="sk-cli-2222"
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob3333")
        p = _project(s)
        ctx = _build(s, p)
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ctx.config.llm.anthropic.api_key == "sk-glob-1111"
    assert ctx.config.llm.claude_cli.api_key == "sk-cli-2222"
    assert ctx.config.scan_token == "ghp_glob3333"
    assert stored == []
    for secret in ("sk-glob-1111", "sk-cli-2222", "ghp_glob3333"):
        assert secret not in repr(ctx.config) and secret not in repr(ctx)


def test_project_secret_overrides_global_for_that_project_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-0000"
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob0000")
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-a111",
            project_id=a.id,
        )
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_proj-a11", project_id=a.id
        )
        ca, cb = _build(s, a), _build(s, b)
    assert ca.config.llm.anthropic.api_key == "sk-proj-a111"
    assert ca.config.scan_token == "ghp_proj-a11"
    assert cb.config.llm.anthropic.api_key == "sk-glob-0000"
    assert cb.config.scan_token == "ghp_glob0000"


def test_project_overriding_base_url_gets_no_global_key(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111"
        )
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-2222"
        )
        plain, overriding = _project(s, "plain"), _project(s, "over")
        save_layer(
            s,
            overriding.id,
            {"llm": {"openai": {"base_url": "https://attacker.example/v1"}}},
        )
        c_plain, c_over = _build(s, plain), _build(s, overriding)
    assert c_plain.config.llm.openai.api_key == "sk-glob-1111"
    assert c_over.config.llm.openai.base_url == "https://attacker.example/v1"
    assert c_over.config.llm.openai.api_key is None  # never sent to the override
    assert (
        c_over.config.llm.anthropic.api_key == "sk-glob-2222"
    )  # other providers unaffected


def test_project_own_key_is_kept_with_its_own_endpoint(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(s, p.id, {"llm": {"openai": {"base_url": "https://gw.example/v1"}}})
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-mine-1111",
            project_id=p.id,
        )
        assert _build(s, p).config.llm.openai.api_key == "sk-mine-1111"


def test_null_endpoint_is_not_an_override(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111"
        )
        p = _project(s)
        save_layer(s, p.id, {"llm": {"openai": {"base_url": None}}})
        assert _build(s, p).config.llm.openai.api_key == "sk-glob-1111"


def test_unreadable_secret_does_not_fail_the_build_or_fall_back(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-0000"
        )
        p = _project(s)
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-1111",
            project_id=p.id,
        )
        for row in s.exec(select(Secret).where(Secret.project_id == p.id)).all():
            row.ciphertext = "garbage"
        s.flush()
        ctx = _build(s, p)
    # the broken project key is skipped; the global one is NOT silently used instead
    assert ctx.config.llm.anthropic.api_key is None


# ---------------------------------------------------------------------------
# ContextCache
# ---------------------------------------------------------------------------


def test_cache_builds_once_and_rebuilds_after_invalidate(data: Path) -> None:
    cache = ContextCache()
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(s, pid, {"analyze": {"max_workers": 2}})
    first = cache.get(pid)
    assert cache.get(pid) is first
    with portal_db.get_session() as s:
        save_layer(s, pid, {"analyze": {"max_workers": 5}})
    assert cache.get(pid) is first  # stale until invalidated, by design
    cache.invalidate(pid)
    assert cache.get(pid).config.analyze.max_workers == 5
    cache.invalidate()
    assert cache.get(pid) is not first


def test_cache_async_miss_and_unknown_project(data: Path) -> None:
    cache = ContextCache()
    with portal_db.get_session() as s:
        pid = _project(s).id

    async def go() -> None:
        ctx = await cache.aget(pid)
        assert ctx.slug == "demo"
        assert await cache.aget(pid) is ctx
        with pytest.raises(ProjectNotFound):
            await cache.aget(999)

    anyio.run(go)


def test_config_repr_hides_secrets() -> None:
    cfg = Config.from_dict(
        {
            "scan": {"token": "ghp_visible"},
            "llm": {"openai": {"api_key": "sk-visible"}},
        },
        Path("."),
    )
    assert "ghp_visible" not in repr(cfg) and "sk-visible" not in repr(cfg)
    assert cfg.scan_token == "ghp_visible" and cfg.llm.openai.api_key == "sk-visible"


def test_claude_oauth_token_is_a_providerless_secret_project_over_global(
    data: Path,
) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="claude_oauth_token", value="sk-ant-oat-glob1")
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s, kind="claude_oauth_token", value="sk-ant-oat-proja", project_id=a.id
        )
        ca, cb = _build(s, a), _build(s, b)
        with pytest.raises(ValueError, match="has no provider"):
            pw_secrets.put_secret(
                s, kind="claude_oauth_token", provider="claude-cli", value="x"
            )
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ca.config.llm.claude_cli.oauth_token == "sk-ant-oat-proja"
    assert cb.config.llm.claude_cli.oauth_token == "sk-ant-oat-glob1"
    assert stored == []  # a secret never lands in a config layer
    for secret in ("sk-ant-oat-glob1", "sk-ant-oat-proja"):
        assert secret not in repr(ca.config) and secret not in repr(cb)
