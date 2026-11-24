"""A linked checkout's ``plans_dir=`` line follows its platform (plan 61, section 9.2 case 13a).

The linked half: Initialize writes no ``plans_dir=`` line for a ``platform``
project; the first v1 answer whose ``project`` block carries ``plans_dir``
writes it (through :func:`whygraph.portal.linked.save_status`), an
unchanged answer touches no file and runs no ``git``, an older platform
(no ``plans_dir``) leaves the line absent, and a value a one-line
``portal.env`` entry cannot hold is refused on the wire.
"""

# ruff: noqa: F811 -- pytest fixtures imported from sibling tests

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import select

from platform_fake import ORG, PLATFORM_ORIGIN, SLUG, TOKEN, FakePlatform, status_body
from test_portal_app import env  # noqa: F401 -- fixture
from test_portal_linked_scans import linked_project, linked_row
from test_portal_runner import portal, scanner, wait_idle  # noqa: F401 -- fixtures
from whygraph.api_v1 import StatusOut
from whygraph.portal import db as portal_db
from whygraph.portal import linked as linked_mod
from whygraph.portal.linked import LinkedProject
from whygraph.portal.models import Project
from whygraph.portal.platform_client import PlatformHttp, api_origin_for

API_ORIGIN = api_origin_for(PLATFORM_ORIGIN, ORG)
PORTAL_ENV = Path(".whygraph") / "portal.env"


@pytest.fixture(autouse=True)
def _forget_synced() -> Iterator[None]:
    """Project ids repeat across portal databases: start every test clean."""
    linked_mod._synced_plans_dir.clear()
    yield
    linked_mod._synced_plans_dir.clear()


@pytest.fixture
def syncs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str | None]]:
    """Every ``sync_plans_marker`` call ``save_status`` makes (the only file / git work)."""
    calls: list[tuple[Path, str | None]] = []
    real = linked_mod.sync_plans_marker

    def spy(root: Path, plans_dir: str | None) -> str | None:
        calls.append((root, plans_dir))
        return real(root, plans_dir)

    monkeypatch.setattr(linked_mod, "sync_plans_marker", spy)
    return calls


def _project_id(slug: str) -> int:
    with portal_db.get_session() as session:
        project = session.exec(select(Project).where(Project.slug == slug)).one()
        assert project.id is not None
        return project.id


def _remote(fake: FakePlatform, project_id: int) -> LinkedProject:
    fake.add_token()
    return LinkedProject(
        PlatformHttp(
            platform_origin=PLATFORM_ORIGIN,
            api_origin=API_ORIGIN,
            token=TOKEN,
            transport=httpx.MockTransport(fake.handle),
        ),
        slug=SLUG,
        org=ORG,
        platform_origin=PLATFORM_ORIGIN,
        project_id=project_id,
    )


def _env_text(root: Path) -> str:
    return (root / PORTAL_ENV).read_text()


def test_linked_checkout_follows_the_platforms_plans_dir(
    portal: TestClient,
    env: SimpleNamespace,
    syncs: list[tuple[Path, str | None]],
) -> None:
    root = linked_project(portal, env, "lnk")
    wait_idle(portal, "lnk")
    # Initialize of a linked project writes no plans_dir= line.
    assert "plans_dir" not in _env_text(root)

    fake = FakePlatform()
    fake.projects[SLUG] = status_body(plans_dir="docs/plans")
    remote = _remote(fake, _project_id("lnk"))
    remote.overview()
    assert _env_text(root).endswith("plans_dir=docs/plans\n")
    assert _env_text(root).startswith("slug=lnk\n")
    assert len(syncs) == 1

    # The same value again: no file read, no git.
    remote.overview()
    remote.history("sample.py", 5, True)
    assert len(syncs) == 1

    # A new value (indexing switched off on the platform) is followed.
    fake.projects[SLUG]["plans_dir"] = ""
    remote.overview()
    assert _env_text(root).endswith("plans_dir=\n")
    assert len(syncs) == 2


def test_an_older_platform_leaves_the_line_absent(
    portal: TestClient,
    env: SimpleNamespace,
    syncs: list[tuple[Path, str | None]],
) -> None:
    root = linked_project(portal, env, "lnk")
    wait_idle(portal, "lnk")
    remote = _remote(FakePlatform(plans_supported=False), _project_id("lnk"))
    remote.overview()
    assert "plans_dir" not in _env_text(root)
    assert syncs == []


def test_a_plans_dir_with_a_newline_is_refused_and_never_written(
    portal: TestClient,
    env: SimpleNamespace,
    syncs: list[tuple[Path, str | None]],
) -> None:
    with pytest.raises(ValidationError):
        StatusOut.model_validate(status_body(plans_dir="docs\nplans"))
    root = linked_project(portal, env, "lnk")
    wait_idle(portal, "lnk")
    fake = FakePlatform()
    fake.projects[SLUG] = status_body(plans_dir="docs/plans\nport=1")
    _remote(fake, _project_id("lnk")).overview()
    assert "plans_dir" not in _env_text(root)
    assert syncs == []


def test_an_uninitialized_linked_project_writes_nothing(
    portal: TestClient,
    env: SimpleNamespace,
    syncs: list[tuple[Path, str | None]],
) -> None:
    root = env.shared / "raw"
    root.mkdir()
    project_id = linked_row(root, "raw")
    fake = FakePlatform()
    fake.projects[SLUG] = status_body(plans_dir="docs/plans")
    _remote(fake, project_id).overview()
    assert syncs == []
    assert not (root / PORTAL_ENV).exists()
